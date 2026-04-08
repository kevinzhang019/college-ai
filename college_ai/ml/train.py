"""LightGBM training script for college admissions probability model."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import shap
from optuna.integration import LightGBMTunerCV
from sklearn.base import BaseEstimator
from sklearn.calibration import CalibratedClassifierCV, CalibrationDisplay
from sklearn.frozen import FrozenEstimator
from sklearn.inspection import permutation_importance
from sklearn.metrics import (
    auc, brier_score_loss, classification_report, confusion_matrix,
    log_loss, roc_auc_score,
)
from sklearn.model_selection import cross_val_score, StratifiedKFold, train_test_split

from college_ai.ml.bucket_configs import (
    MONOTONE_FEATURE_CONSTRAINTS,
    build_monotone_constraints,
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Feature definitions
NUMERIC_FEATURES = [
    'gpa', 'sat_score', 'acceptance_rate', 'sat_25', 'sat_75', 'sat_avg',
    'act_25', 'act_75', 'enrollment', 'retention_rate', 'graduation_rate',
    'student_faculty_ratio', 'tuition_in_state', 'tuition_out_of_state',
    'pct_white', 'pct_black', 'pct_hispanic', 'pct_asian', 'pct_first_gen',
    'sat_percentile_at_school', 'gpa_vs_expected', 'median_earnings_10yr',
    'yield_rate',
    # Engineered features
    'sat_zscore_at_school', 'gpa_zscore_at_school',
    'gpa_x_acceptance', 'sat_percentile_sq',
    'selectivity_x_sat', 'academic_composite_z', 'competitiveness_index',
    'gpa_x_competitiveness', 'sat_x_competitiveness',
    # Residency interaction features
    'instate_x_public', 'residency_x_acceptance',
    # Overqualification / yield protection
    'sat_excess', 'gpa_excess', 'sat_ratio', 'is_yield_protector',
    'overqualification_index',
    # Binary threshold features
    'sat_above_75th', 'sat_below_25th',
    # Selectivity non-linearity
    'acceptance_rate_sq',
    # Test-optional signal
    'has_test_score',
    # Log transforms
    'log_enrollment', 'log_earnings',
    # Niche grade features (ordinal-encoded)
    'niche_academics_ord', 'niche_value_ord', 'niche_professors_ord',
    'niche_diversity_ord', 'niche_campus_ord', 'niche_overall_ord',
    'niche_rank', 'avg_annual_cost', 'cost_earnings_ratio',
    # Major competitiveness interaction
    'stem_competitive_x_acceptance',
    # Yield protection & fit signals
    'yield_x_overqualification', 'academic_fit', 'holistic_signal', 'sat_range',
]

CATEGORICAL_FEATURES = [
    'ownership', 'selectivity_bucket',
    'residency', 'major',
    'setting', 'major_tier',
]

TARGET = 'admitted'


def load_data(data_path: str) -> pd.DataFrame:
    """Load training data from parquet file."""
    logger.info(f"Loading data from {data_path}")
    df = pd.read_parquet(data_path)
    logger.info(f"Data shape: {df.shape}")
    logger.info(f"Columns: {df.columns.tolist()}")
    return df


def preprocess_data(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Preprocess data: handle missing values, encode categoricals.

    Uses LightGBM native categorical support: encodes categories as non-negative
    integer codes via pandas Categorical. NaN stays as NaN — LightGBM learns
    optimal split directions for missing values natively.

    Returns:
        df: preprocessed dataframe
        category_mappings: dict mapping col -> {category: code} for inference
    """
    logger.info("Preprocessing data...")
    df = df.copy()

    # Handle missing values in numeric features
    for col in NUMERIC_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna(df[col].median())

    # Encode categoricals as integer codes for LightGBM native categorical support.
    # NaN stays as NaN (LightGBM handles missing natively).
    category_mappings: Dict[str, Dict[str, int]] = {}
    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            # Convert to pandas Categorical — NaN stays as NaN
            cat = pd.Categorical(df[col])
            # Build mapping: category_value -> integer code
            mapping = {cat: code for code, cat in enumerate(cat.categories)}
            category_mappings[col] = mapping
            # .codes gives -1 for NaN; replace -1 with NaN for LightGBM
            codes = cat.codes.astype(float)
            codes[codes < 0] = float("nan")
            df[col] = codes
            logger.info(
                f"  {col}: {len(mapping)} categories, "
                f"{df[col].isna().sum()} NaN ({df[col].isna().mean():.1%})"
            )

    logger.info("Data preprocessing complete")
    return df, category_mappings


def prepare_features(
    df: pd.DataFrame,
    category_mappings: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[int], List[str]]:
    """
    Prepare feature matrix and get categorical feature indices.

    Returns:
        X: feature matrix
        categorical_indices: indices of categorical features in X
        feature_names: names of all features
    """
    # Select features (both numeric and categorical)
    available_numeric = [f for f in NUMERIC_FEATURES if f in df.columns]
    available_categorical = [f for f in CATEGORICAL_FEATURES if f in df.columns]

    feature_cols = available_numeric + available_categorical
    X = df[feature_cols].copy()

    # Get categorical feature indices (relative to X columns)
    categorical_indices = [
        X.columns.get_loc(col) for col in available_categorical
    ]

    feature_names = X.columns.tolist()

    logger.info(f"Features: {len(feature_names)} total")
    logger.info(f"  Numeric: {len(available_numeric)}")
    logger.info(f"  Categorical: {len(available_categorical)}")
    logger.info(f"Categorical feature indices: {categorical_indices}")

    return X, categorical_indices, feature_names


def split_data(
    X: pd.DataFrame,
    y: pd.Series,
    stratify_col: Optional[pd.Series] = None,
    test_size: float = 0.2,
    random_state: int = 42,
    groups: Optional[pd.Series] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    """
    Split data into train, validation, and test sets.

    If groups is provided, uses StratifiedGroupKFold so that no group
    (e.g. school) appears in multiple splits. This prevents school-level
    data leakage between train/val/test.

    Returns:
        X_train, X_val, X_test, y_train, y_val, y_test
    """
    if groups is not None:
        from sklearn.model_selection import StratifiedGroupKFold
        logger.info("Splitting data (60/20/20 train/val/test) with group-aware splits...")

        # First split: ~80% train, ~20% test using 5-fold (each fold ≈ 20%)
        sgkf = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=random_state)
        train_idx, test_idx = next(iter(sgkf.split(X, y, groups=groups)))
        X_train_full = X.iloc[train_idx]
        X_test = X.iloc[test_idx]
        y_train_full = y.iloc[train_idx]
        y_test = y.iloc[test_idx]
        groups_train = groups.iloc[train_idx]

        # Second split: ~75/25 of train → 60/20 overall
        sgkf2 = StratifiedGroupKFold(n_splits=4, shuffle=True, random_state=random_state)
        train_idx2, val_idx2 = next(iter(sgkf2.split(X_train_full, y_train_full, groups=groups_train)))
        X_train = X_train_full.iloc[train_idx2]
        X_val = X_train_full.iloc[val_idx2]
        y_train = y_train_full.iloc[train_idx2]
        y_val = y_train_full.iloc[val_idx2]

        # Verify no group overlap
        train_groups = set(groups.iloc[X_train.index].unique())
        val_groups = set(groups.iloc[X_val.index].unique())
        test_groups = set(groups.iloc[X_test.index].unique())
        overlap_tv = train_groups & val_groups
        overlap_tt = train_groups & test_groups
        if overlap_tv or overlap_tt:
            logger.warning(
                f"Group overlap detected! train∩val={len(overlap_tv)}, train∩test={len(overlap_tt)}"
            )
        else:
            logger.info(
                f"Group-aware split: {len(train_groups)} train groups, "
                f"{len(val_groups)} val groups, {len(test_groups)} test groups — no overlap"
            )
    else:
        logger.info("Splitting data (60/20/20 train/val/test)...")

        # First split: 80% train, 20% test
        X_train_full, X_test, y_train_full, y_test = train_test_split(
            X, y,
            test_size=test_size,
            random_state=random_state,
            stratify=stratify_col,
        )

        # Split training set into train and validation (75/25 of train = 60/20 of total)
        X_train, X_val, y_train, y_val = train_test_split(
            X_train_full, y_train_full,
            test_size=0.25,
            random_state=random_state,
            stratify=y_train_full,
        )

    logger.info(f"Train set size: {X_train.shape[0]}")
    logger.info(f"Val set size: {X_val.shape[0]}")
    logger.info(f"Test set size: {X_test.shape[0]}")
    logger.info(f"Class distribution - Train: {y_train.value_counts().to_dict()}")

    return X_train, X_val, X_test, y_train, y_val, y_test


def tune_hyperparameters(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    categorical_indices: List[int],
    feature_names: List[str],
    n_trials: int = 50,
    random_state: int = 42,
    is_unbalanced: bool = False,
    monotone_constraints: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Hyperparameter tuning using Optuna's LightGBMTunerCV.

    Uses step-wise tuning optimized for LightGBM (learning_rate →
    feature_fraction → num_leaves → bagging → regularization).
    Optimizes for binary_logloss for better probability calibration.
    """
    logger.info("Starting hyperparameter tuning with LightGBMTunerCV...")

    params = {
        'objective': 'binary',
        'metric': 'binary_logloss',
        'learning_rate': 0.05,
        'max_bin': 127,
        'cat_smooth': 10,
        'min_data_per_group': 50,
        'feature_pre_filter': True,
        'force_col_wise': True,
        'num_threads': os.cpu_count() or 4,
        'is_unbalance': is_unbalanced,
        'random_state': random_state,
        'verbose': -1,
    }

    if monotone_constraints is not None:
        params['monotone_constraints'] = monotone_constraints
        params['monotone_constraints_method'] = 'intermediate'

    train_data = lgb.Dataset(
        X_train,
        label=y_train,
        categorical_feature=categorical_indices,
        feature_name=feature_names,
        free_raw_data=False,
    )

    tuner = LightGBMTunerCV(
        params,
        train_data,
        num_boost_round=1000,
        nfold=3,
        stratified=True,
        callbacks=[
            lgb.early_stopping(stopping_rounds=30, verbose=False),
            lgb.log_evaluation(period=0),
        ],
        optuna_seed=random_state,
        show_progress_bar=True,
    )

    tuner.run()

    best_params = tuner.best_params
    best_score = tuner.best_score

    logger.info(f"Best log loss: {best_score:.4f}")
    logger.info(f"Best parameters: {best_params}")

    return best_params


def tune_hyperparameters_brier(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    categorical_indices: List[int],
    feature_names: List[str],
    n_trials: int = 100,
    random_state: int = 42,
    is_unbalanced: bool = False,
    monotone_constraints: Optional[List[int]] = None,
    groups: Optional[pd.Series] = None,
    multi_objective: bool = False,
) -> Dict[str, Any]:
    """Custom Optuna hyperparameter tuning optimizing Brier score.

    Unlike LightGBMTunerCV (which hardcodes log loss), this directly
    optimizes for calibrated probabilities. Optionally uses multi-objective
    optimization (AUC + Brier) with NSGA-II to find the Pareto front.

    Args:
        multi_objective: If True, optimize both AUC and Brier score
                         simultaneously using NSGA-II sampler.
    """
    import optuna
    from optuna.samplers import TPESampler, NSGAIISampler
    from optuna.pruners import MedianPruner

    logger.info(
        f"Starting custom Optuna tuning ({'multi-objective' if multi_objective else 'Brier'})..."
    )

    # Use StratifiedGroupKFold if groups provided, else StratifiedKFold
    if groups is not None:
        from sklearn.model_selection import StratifiedGroupKFold
        cv = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=random_state)
        cv_split_args = (X_train, y_train, groups)
    else:
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=random_state)
        cv_split_args = (X_train, y_train)

    def objective(trial):
        params = {
            'objective': 'binary',
            'metric': 'binary_logloss',
            'verbosity': -1,
            'boosting_type': 'gbdt',
            'force_col_wise': True,
            'num_threads': os.cpu_count() or 4,
            'is_unbalance': is_unbalanced,
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.2, log=True),
            'num_leaves': trial.suggest_int('num_leaves', 16, 128),
            'max_depth': trial.suggest_int('max_depth', 3, 10),
            'min_child_samples': trial.suggest_int('min_child_samples', 10, 100),
            'feature_fraction': trial.suggest_float('feature_fraction', 0.5, 1.0),
            'bagging_fraction': trial.suggest_float('bagging_fraction', 0.5, 1.0),
            'bagging_freq': trial.suggest_int('bagging_freq', 1, 7),
            'reg_alpha': trial.suggest_float('reg_alpha', 1e-6, 10.0, log=True),
            'reg_lambda': trial.suggest_float('reg_lambda', 1e-6, 10.0, log=True),
            'min_split_gain': trial.suggest_float('min_split_gain', 0.0, 0.5),
            'path_smooth': trial.suggest_float('path_smooth', 0.0, 80.0),
            'min_sum_hessian_in_leaf': trial.suggest_float('min_sum_hessian_in_leaf', 1e-3, 30.0, log=True),
            'max_cat_threshold': 64,
            'cat_smooth': trial.suggest_float('cat_smooth', 5.0, 50.0),
        }

        if monotone_constraints is not None:
            params['monotone_constraints'] = monotone_constraints
            params['monotone_constraints_method'] = 'intermediate'

        brier_scores = []
        auc_scores = []

        for fold, (train_idx, val_idx) in enumerate(cv.split(*cv_split_args)):
            X_fold_train = X_train.iloc[train_idx]
            X_fold_val = X_train.iloc[val_idx]
            y_fold_train = y_train.iloc[train_idx]
            y_fold_val = y_train.iloc[val_idx]

            train_data = lgb.Dataset(
                X_fold_train, label=y_fold_train,
                categorical_feature=categorical_indices,
                feature_name=feature_names, free_raw_data=False,
            )
            val_data = lgb.Dataset(
                X_fold_val, label=y_fold_val, reference=train_data,
                categorical_feature=categorical_indices,
                feature_name=feature_names, free_raw_data=False,
            )

            model = lgb.train(
                params, train_data, num_boost_round=1000,
                valid_sets=[val_data], valid_names=["valid"],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=30, verbose=False),
                    lgb.log_evaluation(period=0),
                ],
            )

            probs = model.predict(X_fold_val)
            brier_scores.append(brier_score_loss(y_fold_val, probs))
            if multi_objective:
                auc_scores.append(roc_auc_score(y_fold_val, probs))

        if multi_objective:
            return -np.mean(auc_scores), np.mean(brier_scores)
        return np.mean(brier_scores)

    if multi_objective:
        study = optuna.create_study(
            directions=["minimize", "minimize"],
            sampler=NSGAIISampler(seed=random_state),
        )
        study.set_metric_names(["neg_AUC", "brier_score"])
        study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

        # Pick the trial with best AUC among those with Brier < median
        pareto_trials = study.best_trials
        if pareto_trials:
            brier_median = np.median([t.values[1] for t in pareto_trials])
            good_trials = [t for t in pareto_trials if t.values[1] <= brier_median]
            best_trial = min(good_trials, key=lambda t: t.values[0])
            logger.info(
                f"Selected Pareto trial: AUC={-best_trial.values[0]:.4f}, "
                f"Brier={best_trial.values[1]:.4f}"
            )
            best_params = best_trial.params
        else:
            best_params = study.best_trials[0].params
    else:
        study = optuna.create_study(
            direction="minimize",
            sampler=TPESampler(seed=random_state),
            pruner=MedianPruner(n_startup_trials=10, n_warmup_steps=20),
        )
        study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
        best_params = study.best_params
        logger.info(f"Best Brier score: {study.best_value:.4f}")

    logger.info(f"Best parameters: {best_params}")
    return best_params


def get_default_params(is_unbalanced: bool = False) -> Dict[str, Any]:
    """Get reasonable default hyperparameters with speed optimizations."""
    return {
        'objective': 'binary',
        'metric': 'binary_logloss',
        'learning_rate': 0.05,
        'num_leaves': 64,
        'max_depth': 7,
        'min_child_samples': 20,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'reg_alpha': 0.05,
        'reg_lambda': 0.05,
        'min_gain_to_split': 0.01,
        'bagging_freq': 5,
        'max_bin': 127,
        'cat_smooth': 10,
        'min_data_per_group': 50,
        'feature_pre_filter': True,
        'force_col_wise': True,
        'num_threads': os.cpu_count() or 4,
        'is_unbalance': is_unbalanced,
        'random_state': 42,
        'verbose': -1,
        'path_smooth': 15,
        'min_sum_hessian_in_leaf': 10,
        'max_cat_threshold': 64,
    }


def train_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    categorical_indices: List[int],
    feature_names: List[str],
    params: Dict[str, Any],
    is_unbalanced: bool = False,
) -> lgb.Booster:
    """Train LightGBM model on full training set."""
    logger.info("Training final model with best parameters...")

    # Merge tuned params with speed/fixed params
    full_params = {
        'objective': 'binary',
        'metric': 'binary_logloss',
        'learning_rate': 0.05,
        'max_bin': 127,
        'cat_smooth': 10,
        'min_data_per_group': 50,
        'feature_pre_filter': True,
        'force_col_wise': True,
        'num_threads': os.cpu_count() or 4,
        'is_unbalance': is_unbalanced,
        'verbose': -1,
        **params,
    }

    train_data = lgb.Dataset(
        X_train,
        label=y_train,
        categorical_feature=categorical_indices,
        feature_name=feature_names,
        free_raw_data=False,
    )

    val_data = lgb.Dataset(
        X_val,
        label=y_val,
        reference=train_data,
        categorical_feature=categorical_indices,
        feature_name=feature_names,
        free_raw_data=False,
    )

    model = lgb.train(
        full_params,
        train_data,
        num_boost_round=2000,
        valid_sets=[train_data, val_data],
        valid_names=['train', 'valid'],
        callbacks=[
            lgb.early_stopping(stopping_rounds=50, verbose=True),
            lgb.log_evaluation(period=100),
        ],
    )

    logger.info(f"Model trained with {model.num_trees()} trees")

    # Log gain-based feature importances (more reliable than split-based)
    gain_imp = model.feature_importance(importance_type='gain')
    imp_df = pd.DataFrame({
        'feature': feature_names,
        'gain': gain_imp,
    }).sort_values('gain', ascending=False)
    logger.info("\nFeature importance (gain):")
    for _, row in imp_df.head(20).iterrows():
        logger.info(f"  {row['feature']:35s} {row['gain']:>12.1f}")
    zero_gain = imp_df[imp_df['gain'] == 0]
    if len(zero_gain) > 0:
        logger.info(f"\n  {len(zero_gain)} features with zero gain: {zero_gain['feature'].tolist()}")

    return model


class LGBWrapper(BaseEstimator):
    """Minimal sklearn-compatible wrapper around a LightGBM Booster."""
    _estimator_type = "classifier"
    classes_ = np.array([0, 1])

    def __init__(self, booster: lgb.Booster):
        self.booster = booster
        self.is_fitted_ = True

    def fit(self, X, y=None):
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        proba = self.booster.predict(X)
        return np.column_stack([1 - proba, proba])

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return (self.booster.predict(X) > 0.5).astype(int)


def calibrate_model(
    model,
    X_cal: pd.DataFrame,
    y_cal: pd.Series,
    method: str = "isotonic",
):
    """Apply probability calibration using a held-out calibration set.

    Args:
        method: 'isotonic', 'sigmoid', or 'venn_abers'.
                'venn_abers' uses Venn-ABERS inductive calibration which has
                provable finite-sample validity — best for imbalanced buckets.
    """
    logger.info(f"Calibrating model with {method}...")

    if method == "venn_abers":
        from venn_abers import VennAbersCalibrator
        wrapper = LGBWrapper(model) if isinstance(model, lgb.Booster) else model
        calibrator = VennAbersCalibrator(
            estimator=FrozenEstimator(wrapper),
            inductive=True,
            cal_size=0.5,
        )
        calibrator.fit(X_cal, y_cal)
    else:
        wrapper = LGBWrapper(model) if isinstance(model, lgb.Booster) else model
        calibrator = CalibratedClassifierCV(
            estimator=FrozenEstimator(wrapper),
            method=method,
        )
        calibrator.fit(X_cal, y_cal)

    logger.info("Model calibration complete")
    return calibrator


def calibrate_model_best(
    model,
    X_cal: pd.DataFrame,
    y_cal: pd.Series,
):
    """Try isotonic, sigmoid, and Venn-ABERS calibration, pick the one with lower Brier score."""
    wrapper = LGBWrapper(model) if isinstance(model, lgb.Booster) else model
    raw_proba = wrapper.predict_proba(X_cal)[:, 1]
    raw_brier = brier_score_loss(y_cal, raw_proba)
    logger.info(f"Raw model Brier on calibration set: {raw_brier:.4f}")

    best_calibrator = None
    best_brier = float("inf")
    best_method = ""

    for method in ("isotonic", "sigmoid", "venn_abers"):
        try:
            calibrator = calibrate_model(model, X_cal, y_cal, method=method)
            cal_proba = calibrator.predict_proba(X_cal)[:, 1]
            cal_brier = brier_score_loss(y_cal, cal_proba)
            cal_ece = _expected_calibration_error(y_cal.values, cal_proba)
            logger.info(f"  {method:12s} calibration — Brier: {cal_brier:.4f}, ECE: {cal_ece:.4f}")
            if cal_brier < best_brier:
                best_brier = cal_brier
                best_calibrator = calibrator
                best_method = method
        except Exception as e:
            logger.warning(f"  {method:12s} calibration failed: {e}")

    logger.info(f"Selected {best_method} calibration (Brier: {best_brier:.4f})")
    return best_calibrator


def evaluate_calibrated_model(
    calibrator: CalibratedClassifierCV,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    selectivity_buckets: Optional[pd.Series] = None,
) -> None:
    """Evaluate calibrated model on test set."""
    logger.info("\n" + "=" * 60)
    logger.info("CALIBRATED MODEL EVALUATION ON TEST SET")
    logger.info("=" * 60)

    y_pred_proba = calibrator.predict_proba(X_test)[:, 1]
    y_pred = (y_pred_proba > 0.5).astype(int)

    auc_roc = roc_auc_score(y_test, y_pred_proba)
    brier = brier_score_loss(y_test, y_pred_proba)
    logloss = log_loss(y_test, y_pred_proba)
    ece = _expected_calibration_error(y_test.values, y_pred_proba)
    base_rate = y_test.mean()
    brier_clim = base_rate * (1 - base_rate)
    bss = 1 - (brier / brier_clim) if brier_clim > 0 else 0.0

    logger.info(f"AUC-ROC:   {auc_roc:.4f}")
    logger.info(f"Brier:     {brier:.4f}")
    logger.info(f"BSS:       {bss:.4f}")
    logger.info(f"Log Loss:  {logloss:.4f}")
    logger.info(f"ECE:       {ece:.4f}")

    if selectivity_buckets is not None:
        logger.info("\nPer-selectivity-bucket Brier / BSS scores (calibrated):")
        for bucket in sorted(selectivity_buckets.unique()):
            mask = selectivity_buckets == bucket
            if mask.sum() > 0:
                bucket_brier = brier_score_loss(y_test[mask], y_pred_proba[mask])
                bucket_base = y_test[mask].mean()
                bucket_clim = bucket_base * (1 - bucket_base)
                bucket_bss = 1 - (bucket_brier / bucket_clim) if bucket_clim > 0 else 0.0
                logger.info(
                    f"  {bucket:15s}: Brier={bucket_brier:.4f}  BSS={bucket_bss:.4f}  (n={mask.sum()})"
                )

    logger.info("=" * 60 + "\n")


def generate_reliability_diagrams(
    model: lgb.Booster,
    calibrator: Optional[CalibratedClassifierCV],
    X_test: pd.DataFrame,
    y_test: pd.Series,
    selectivity_buckets: Optional[pd.Series] = None,
    output_dir: str = ".",
) -> None:
    """Generate and save reliability diagrams for raw and calibrated models."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Overall reliability diagram: raw vs calibrated
    fig, ax = plt.subplots(1, 1, figsize=(8, 8))
    raw_proba = model.predict(X_test)
    CalibrationDisplay.from_predictions(
        y_test, raw_proba, n_bins=10, name="Raw LightGBM", ax=ax,
    )
    if calibrator is not None:
        cal_proba = calibrator.predict_proba(X_test)[:, 1]
        CalibrationDisplay.from_predictions(
            y_test, cal_proba, n_bins=10, name="Calibrated", ax=ax,
        )
    ax.set_title("Reliability Diagram")
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "reliability_diagram.png"), dpi=200)
    plt.close(fig)
    logger.info(f"Reliability diagram saved to {output_dir}/reliability_diagram.png")

    # Per-bucket reliability diagrams
    if selectivity_buckets is not None and calibrator is not None:
        buckets = sorted(selectivity_buckets.unique())
        fig, axes = plt.subplots(1, len(buckets), figsize=(5 * len(buckets), 5))
        if len(buckets) == 1:
            axes = [axes]
        cal_proba = calibrator.predict_proba(X_test)[:, 1]
        for ax, bucket in zip(axes, buckets):
            mask = selectivity_buckets == bucket
            if mask.sum() < 10:
                ax.set_title(f"{bucket} (n={mask.sum()}, too few)")
                continue
            CalibrationDisplay.from_predictions(
                y_test[mask], cal_proba[mask], n_bins=10,
                name=bucket, ax=ax,
            )
            ax.set_title(f"{bucket} (n={mask.sum()})")
        fig.suptitle("Per-Bucket Reliability Diagrams (Calibrated)")
        fig.tight_layout()
        fig.savefig(os.path.join(output_dir, "reliability_per_bucket.png"), dpi=200)
        plt.close(fig)
        logger.info(f"Per-bucket reliability diagram saved to {output_dir}/reliability_per_bucket.png")


def run_permutation_importance(
    model: lgb.Booster,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    feature_names: List[str],
) -> List[str]:
    """Run permutation importance and log results. Returns list of low-importance features."""
    logger.info("\nRunning permutation importance (scoring=neg_log_loss, n_repeats=10)...")
    wrapper = LGBWrapper(model)
    result = permutation_importance(
        wrapper, X_test, y_test,
        scoring='neg_log_loss',
        n_repeats=10,
        random_state=42,
        n_jobs=-1,
    )

    imp_df = pd.DataFrame({
        'feature': feature_names,
        'importance_mean': result.importances_mean,
        'importance_std': result.importances_std,
    }).sort_values('importance_mean', ascending=False)

    logger.info("\nPermutation importance (neg_log_loss, higher = more important):")
    for _, row in imp_df.iterrows():
        flag = " *LOW*" if row['importance_mean'] < 0.0005 else ""
        logger.info(
            f"  {row['feature']:35s} {row['importance_mean']:>10.5f} "
            f"(+/- {row['importance_std']:.5f}){flag}"
        )

    low_importance = imp_df[imp_df['importance_mean'] < 0.0005]['feature'].tolist()
    if low_importance:
        logger.info(f"\n{len(low_importance)} features with near-zero permutation importance:")
        for f in low_importance:
            logger.info(f"  - {f}")

    return low_importance


def _expected_calibration_error(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> float:
    """Compute Expected Calibration Error (ECE) with equal-width bins."""
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        if mask.sum() == 0:
            continue
        bin_acc = y_true[mask].mean()
        bin_conf = y_prob[mask].mean()
        ece += mask.sum() * abs(bin_acc - bin_conf)
    return ece / len(y_true)


def evaluate_model(
    model: lgb.Booster,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    selectivity_buckets: Optional[pd.Series] = None,
) -> None:
    """Evaluate model on test set with comprehensive metrics."""
    logger.info("\n" + "="*60)
    logger.info("MODEL EVALUATION ON TEST SET")
    logger.info("="*60)

    # Get predictions
    y_pred_proba = model.predict(X_test)
    y_pred = (y_pred_proba > 0.5).astype(int)

    # Core metrics
    auc_roc = roc_auc_score(y_test, y_pred_proba)
    brier = brier_score_loss(y_test, y_pred_proba)
    logloss = log_loss(y_test, y_pred_proba)
    ece = _expected_calibration_error(y_test.values, y_pred_proba)

    # Brier Skill Score (comparable across buckets with different base rates)
    base_rate = y_test.mean()
    brier_climatology = base_rate * (1 - base_rate)
    bss = 1 - (brier / brier_climatology) if brier_climatology > 0 else 0.0

    logger.info(f"AUC-ROC:   {auc_roc:.4f}")
    logger.info(f"Brier:     {brier:.4f}")
    logger.info(f"BSS:       {bss:.4f}")
    logger.info(f"Log Loss:  {logloss:.4f}")
    logger.info(f"ECE:       {ece:.4f}")

    # Per-selectivity-bucket Brier scores
    if selectivity_buckets is not None:
        logger.info("\nPer-selectivity-bucket Brier / BSS scores:")
        for bucket in sorted(selectivity_buckets.unique()):
            mask = selectivity_buckets == bucket
            if mask.sum() > 0:
                bucket_brier = brier_score_loss(y_test[mask], y_pred_proba[mask])
                bucket_base = y_test[mask].mean()
                bucket_clim = bucket_base * (1 - bucket_base)
                bucket_bss = 1 - (bucket_brier / bucket_clim) if bucket_clim > 0 else 0.0
                logger.info(
                    f"  {bucket:15s}: Brier={bucket_brier:.4f}  BSS={bucket_bss:.4f}  (n={mask.sum()})"
                )

    # Classification report
    logger.info("\nClassification Report:")
    logger.info("\n" + classification_report(y_test, y_pred,
                                            target_names=['Rejected', 'Accepted']))

    # Confusion matrix
    cm = confusion_matrix(y_test, y_pred)
    logger.info(f"\nConfusion Matrix:")
    logger.info(f"  True Negatives:  {cm[0, 0]}")
    logger.info(f"  False Positives: {cm[0, 1]}")
    logger.info(f"  False Negatives: {cm[1, 0]}")
    logger.info(f"  True Positives:  {cm[1, 1]}")
    logger.info("="*60 + "\n")


def generate_shap_summary(
    model: lgb.Booster,
    X_test: pd.DataFrame,
    feature_names: List[str],
    output_path: str
) -> None:
    """Generate SHAP summary plot."""
    logger.info("Generating SHAP summary plot...")

    # Create SHAP explainer
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X_test)

    # For binary classification, shap_values is a list with 2 arrays
    # Use the positive class (index 1)
    if isinstance(shap_values, list):
        shap_values = shap_values[1]

    # Generate summary plot
    import matplotlib.pyplot as plt

    plt.figure(figsize=(12, 8))
    shap.summary_plot(shap_values, X_test, feature_names=feature_names, show=False)
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()

    logger.info(f"SHAP summary plot saved to {output_path}")


def compute_target_encoding(
    school_ids: pd.Series,
    y: pd.Series,
    smoothing: int = 300,
    n_folds: int = 5,
    random_state: int = 42,
    groups: Optional[pd.Series] = None,
) -> Tuple[pd.Series, Dict[Any, float], float]:
    """Bayesian target encoding with CV to prevent leakage.

    Works for any grouping column (school_id, major, etc.).
    For each fold, the encoding is computed from the other folds only.

    If groups is provided, uses StratifiedGroupKFold to ensure no group
    (e.g. school) appears in both train and val folds — prevents
    school-level data leakage.

    Returns the encoded column, the full encoding map, and global mean.
    """
    global_mean = y.mean()
    encoded = pd.Series(np.nan, index=school_ids.index, dtype=float)

    if groups is not None:
        from sklearn.model_selection import StratifiedGroupKFold
        kf = StratifiedGroupKFold(n_splits=n_folds, shuffle=True, random_state=random_state)
        split_args = (school_ids, y, groups)
    else:
        kf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=random_state)
        split_args = (school_ids, y)
    for train_idx, val_idx in kf.split(*split_args):
        # Compute per-school stats from training fold
        fold_y = y.iloc[train_idx]
        fold_ids = school_ids.iloc[train_idx]
        school_stats = fold_y.groupby(fold_ids).agg(['sum', 'count'])
        school_n = school_stats['count']
        school_mean = school_stats['sum'] / school_n
        smooth_val = (school_n * school_mean + smoothing * global_mean) / (school_n + smoothing)

        # Apply to validation fold
        val_ids = school_ids.iloc[val_idx]
        encoded.iloc[val_idx] = val_ids.map(smooth_val).fillna(global_mean)

    # Build full encoding map for inference
    full_stats = y.groupby(school_ids).agg(['sum', 'count'])
    full_n = full_stats['count']
    full_mean = full_stats['sum'] / full_n
    encoding_map = ((full_n * full_mean + smoothing * global_mean) / (full_n + smoothing)).to_dict()

    logger.info(f"Target encoding: {len(encoding_map)} schools, global mean={global_mean:.4f}")
    return encoded, encoding_map, global_mean


def save_model_and_config(
    model,
    calibrator: Optional[CalibratedClassifierCV],
    category_mappings: Dict[str, Dict[str, int]],
    feature_names: List[str],
    categorical_indices: List[int],
    model_dir: str,
    school_avg_admitted_gpa: Optional[Dict[int, float]] = None,
    z_stats: Optional[Dict[str, float]] = None,
    target_encoding_map: Optional[Dict[int, float]] = None,
    target_encoding_global_mean: Optional[float] = None,
    major_encoding_map: Optional[Dict[str, float]] = None,
    major_encoding_global_mean: Optional[float] = None,
    model_type: str = "lightgbm",
) -> None:
    """Save model, calibrator, category mappings, and configuration."""
    logger.info("Saving model and configuration...")

    # Create model directory if it doesn't exist
    Path(model_dir).mkdir(parents=True, exist_ok=True)

    # Save model, calibrator, category mappings, and lookup tables
    model_path = os.path.join(model_dir, 'admissions_lgbm.pkl')
    joblib.dump({
        'model': model,
        'calibrator': calibrator,
        'category_mappings': category_mappings,
        'school_avg_admitted_gpa': school_avg_admitted_gpa or {},
        'z_stats': z_stats,
        'target_encoding_map': target_encoding_map,
        'target_encoding_global_mean': target_encoding_global_mean,
        'major_encoding_map': major_encoding_map,
        'major_encoding_global_mean': major_encoding_global_mean,
    }, model_path)
    logger.info(f"Model saved to {model_path}")

    # Save configuration
    config = {
        'feature_names': feature_names,
        'categorical_indices': categorical_indices,
        'numeric_features': NUMERIC_FEATURES,
        'categorical_features': CATEGORICAL_FEATURES,
        'target': TARGET,
        'model_type': model_type,
    }

    config_path = os.path.join(model_dir, 'model_config.json')
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    logger.info(f"Configuration saved to {config_path}")


# ---------------------------------------------------------------------------
# CatBoost training path
# ---------------------------------------------------------------------------

class CatBoostWrapper:
    """Sklearn-compatible wrapper for a CatBoostClassifier for calibration."""
    _estimator_type = "classifier"
    classes_ = np.array([0, 1])

    def __init__(self, model):
        self.model = model
        self.is_fitted_ = True

    def fit(self, X, y=None):
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(X)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict(X)


def _train_catboost(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    X_cal: pd.DataFrame,
    y_cal: pd.Series,
    feature_names: List[str],
    categorical_indices: List[int],
    monotone_constraints: Optional[List[int]] = None,
) -> tuple:
    """Train a CatBoost model as alternative to LightGBM.

    Returns (model_wrapper, calibrator) tuple where model_wrapper has
    a .predict(X) method returning probabilities (for evaluate_model compat).
    """
    try:
        from catboost import CatBoostClassifier, Pool
    except ImportError:
        logger.error("catboost not installed. Run: pip install catboost")
        raise

    logger.info("Training CatBoost model...")

    cat_feature_names = [feature_names[i] for i in categorical_indices]

    # Build monotone constraints dict for CatBoost (feature_index -> direction)
    mc_dict = None
    if monotone_constraints is not None:
        mc_dict = {
            i: v for i, v in enumerate(monotone_constraints) if v != 0
        }

    params = {
        "iterations": 2000,
        "learning_rate": 0.05,
        "depth": 7,
        "l2_leaf_reg": 3.0,
        "loss_function": "Logloss",
        "eval_metric": "Logloss",
        "random_seed": 42,
        "verbose": 100,
        "early_stopping_rounds": 50,
        "cat_features": cat_feature_names,
        "posterior_sampling": True,
        "langevin": True,
    }
    if mc_dict:
        params["monotone_constraints"] = mc_dict

    model = CatBoostClassifier(**params)
    model.fit(
        X_train, y_train,
        eval_set=(X_val, y_val),
        use_best_model=True,
    )

    logger.info(f"CatBoost model trained with {model.tree_count_} trees")

    # Wrap for calibration and eval compatibility
    wrapper = CatBoostWrapper(model)

    # Calibrate
    cal_iso = CalibratedClassifierCV(
        estimator=FrozenEstimator(wrapper), method="isotonic",
    )
    cal_iso.fit(X_cal, y_cal)
    cal_sig = CalibratedClassifierCV(
        estimator=FrozenEstimator(wrapper), method="sigmoid",
    )
    cal_sig.fit(X_cal, y_cal)

    brier_iso = brier_score_loss(y_cal, cal_iso.predict_proba(X_cal)[:, 1])
    brier_sig = brier_score_loss(y_cal, cal_sig.predict_proba(X_cal)[:, 1])
    logger.info(f"CatBoost calibration — isotonic Brier: {brier_iso:.4f}, sigmoid Brier: {brier_sig:.4f}")
    calibrator = cal_iso if brier_iso <= brier_sig else cal_sig

    # Return a duck-typed model that evaluate_model can call .predict(X) on
    return CatBoostRawPredictor(model), calibrator


class CatBoostRawPredictor:
    """Duck-type to match lgb.Booster.predict() signature for evaluate_model compat."""
    def __init__(self, cb_model):
        self._model = cb_model

    def predict(self, X):
        return self._model.predict_proba(X)[:, 1]

    def feature_importance(self, importance_type='gain'):
        return self._model.get_feature_importance()

    def num_trees(self):
        return self._model.tree_count_


def main() -> None:
    """Main training pipeline."""
    parser = argparse.ArgumentParser(
        description='Train LightGBM model for college admissions prediction'
    )
    parser.add_argument(
        '--skip-tuning',
        action='store_true',
        help='Skip hyperparameter tuning and use default parameters'
    )
    parser.add_argument(
        '--data-path',
        type=str,
        default='data/training_data.parquet',
        help='Path to training data parquet file'
    )
    parser.add_argument(
        '--model-dir',
        type=str,
        default='model',
        help='Directory to save model and config'
    )
    parser.add_argument(
        '--n-trials',
        type=int,
        default=50,
        help='Number of Optuna trials for hyperparameter tuning'
    )
    parser.add_argument(
        '--force-imbalance-correction',
        action='store_true',
        help='Enable is_unbalance flag (off by default — hurts calibration)'
    )
    parser.add_argument(
        '--prune-features',
        action='store_true',
        help='Remove features with near-zero permutation importance and retrain'
    )
    parser.add_argument(
        '--model-type',
        type=str,
        choices=['lightgbm', 'catboost'],
        default='lightgbm',
        help='Model type to train (default: lightgbm)'
    )
    parser.add_argument(
        '--tune-brier',
        action='store_true',
        help='Use custom Optuna tuning optimizing Brier score instead of LightGBMTunerCV'
    )
    parser.add_argument(
        '--multi-objective',
        action='store_true',
        help='Use multi-objective Optuna (AUC + Brier) with NSGA-II'
    )

    args = parser.parse_args()

    logger.info("Starting college admissions model training pipeline")
    logger.info(f"Data path: {args.data_path}")
    logger.info(f"Model directory: {args.model_dir}")

    # Load and preprocess data
    df = load_data(args.data_path)

    # --- Target encoding for school_id (before preprocessing drops it) ---
    # Use StratifiedGroupKFold with groups=school_id to prevent school-level leakage
    target_encoding_map = None
    target_encoding_global_mean = None
    school_ids_for_split = None   # save for grouped train/test split
    if 'school_id' in df.columns and TARGET in df.columns:
        school_ids_for_split = df['school_id'].copy()
        y_for_te = df[TARGET].astype(int)
        te_encoded, target_encoding_map, target_encoding_global_mean = compute_target_encoding(
            school_ids_for_split, y_for_te,
            groups=school_ids_for_split,
        )
        df['school_target_encoded'] = te_encoded

    # --- Target encoding for major (smoothed per-major admission rate) ---
    # Group by school_id to prevent school-level leakage
    major_encoding_map = None
    major_encoding_global_mean = None
    if 'major' in df.columns and TARGET in df.columns:
        major_notna = df['major'].notna()
        if major_notna.any():
            major_groups = school_ids_for_split.loc[major_notna] if school_ids_for_split is not None else None
            te_major, major_encoding_map, major_encoding_global_mean = compute_target_encoding(
                df.loc[major_notna, 'major'],
                df.loc[major_notna, TARGET].astype(int),
                smoothing=100,
                groups=major_groups,
            )
            df['major_target_encoded'] = float("nan")
            df.loc[major_notna, 'major_target_encoded'] = te_major
            logger.info(f"Major target encoding: {len(major_encoding_map)} majors")

    # Save original selectivity_bucket before it gets encoded to integers
    selectivity_buckets = None
    if 'selectivity_bucket' in df.columns:
        selectivity_buckets = df['selectivity_bucket'].copy()

    df, category_mappings = preprocess_data(df)

    # --- Compute z-stats from full training data (before split) for academic_composite_z ---
    z_stats = None
    if 'academic_composite_z' in df.columns:
        # z_stats were already computed during feature engineering in data_pipeline;
        # recompute here from the exported data for saving to model pickle
        z_stats = {
            "gpa_mean": df["gpa"].mean(),
            "gpa_std": df["gpa"].std(),
            "sat_mean": df["sat_score"].mean(),
            "sat_std": df["sat_score"].std(),
        }
        logger.info(f"Z-stats for inference: {z_stats}")

    # Add school_target_encoded to numeric features if present
    if 'school_target_encoded' in df.columns:
        if 'school_target_encoded' not in NUMERIC_FEATURES:
            NUMERIC_FEATURES.append('school_target_encoded')

    # Add major_target_encoded to numeric features if present
    if 'major_target_encoded' in df.columns:
        if 'major_target_encoded' not in NUMERIC_FEATURES:
            NUMERIC_FEATURES.append('major_target_encoded')

    # Prepare features and target
    X, categorical_indices, feature_names = prepare_features(df, category_mappings)
    y = df[TARGET].astype(int)

    # Build monotone constraints from feature names
    mc = build_monotone_constraints(feature_names)
    n_constrained = sum(1 for c in mc if c != 0)
    logger.info(f"Monotone constraints: {n_constrained}/{len(mc)} features constrained")

    # Split data — use group-aware splits if school_id is available
    stratify_col = selectivity_buckets if selectivity_buckets is not None else y
    X_train, X_val, X_test, y_train, y_val, y_test = split_data(
        X, y, stratify_col=stratify_col,
        groups=school_ids_for_split,
    )

    # Align selectivity buckets with test set
    test_buckets = selectivity_buckets.loc[y_test.index] if selectivity_buckets is not None else None

    # Use half of validation set for calibration
    cal_split = len(X_val) // 2
    X_cal, X_val = X_val.iloc[:cal_split], X_val.iloc[cal_split:]
    y_cal, y_val = y_val.iloc[:cal_split], y_val.iloc[cal_split:]

    # Class imbalance: is_unbalance is OFF by default — A/B testing showed
    # lower log loss and higher AUC without it (it distorts calibrated probabilities)
    pos_rate = y_train.mean()
    is_unbalanced = args.force_imbalance_correction
    if is_unbalanced:
        logger.info(f"is_unbalance forced ON (positive rate: {pos_rate:.1%})")
    else:
        logger.info(f"is_unbalance OFF (positive rate: {pos_rate:.1%})")

    # Hyperparameter tuning or use defaults
    if args.skip_tuning:
        logger.info("Using default hyperparameters (skip-tuning flag set)")
        best_params = get_default_params(is_unbalanced=is_unbalanced)
    elif args.tune_brier or args.multi_objective:
        # Custom Optuna tuning optimizing Brier score (or multi-objective AUC + Brier)
        train_groups = school_ids_for_split.loc[X_train.index] if school_ids_for_split is not None else None
        best_params = tune_hyperparameters_brier(
            X_train, y_train, categorical_indices, feature_names,
            n_trials=args.n_trials, is_unbalanced=is_unbalanced,
            monotone_constraints=mc,
            groups=train_groups,
            multi_objective=args.multi_objective,
        )
    else:
        best_params = tune_hyperparameters(
            X_train, y_train, categorical_indices, feature_names,
            n_trials=args.n_trials, is_unbalanced=is_unbalanced,
            monotone_constraints=mc,
        )

    # Inject monotone constraints into training params
    best_params['monotone_constraints'] = mc
    best_params['monotone_constraints_method'] = 'intermediate'

    # Train model (LightGBM or CatBoost)
    if args.model_type == 'catboost':
        model, calibrator = _train_catboost(
            X_train, y_train, X_val, y_val, X_cal, y_cal,
            feature_names, categorical_indices, mc,
        )
    else:
        model = train_model(
            X_train, y_train, X_val, y_val,
            categorical_indices, feature_names, best_params,
            is_unbalanced=is_unbalanced,
        )
        # Calibrate: compare isotonic vs sigmoid, pick the better one
        calibrator = calibrate_model_best(model, X_cal, y_cal)

    # Evaluate raw model
    logger.info("\n--- RAW MODEL EVALUATION ---")
    evaluate_model(model, X_test, y_test, selectivity_buckets=test_buckets)

    # Evaluate calibrated model
    if calibrator is not None:
        logger.info("\n--- CALIBRATED MODEL EVALUATION ---")
        evaluate_calibrated_model(calibrator, X_test, y_test, selectivity_buckets=test_buckets)

    # Generate reliability diagrams
    generate_reliability_diagrams(
        model, calibrator, X_test, y_test,
        selectivity_buckets=test_buckets,
        output_dir=args.model_dir,
    )

    # Permutation importance analysis
    low_imp_features = run_permutation_importance(model, X_test, y_test, feature_names)

    # Generate SHAP summary
    shap_output = os.path.join(args.model_dir, 'shap_summary.png')
    generate_shap_summary(model, X_test.head(100), feature_names, shap_output)

    # Extract per-school avg admitted GPA lookup for inference
    school_avg_admitted_gpa = None
    if 'school_avg_admitted_gpa' in df.columns:
        gpa_lookup = df.dropna(subset=['school_avg_admitted_gpa'])
        school_avg_admitted_gpa = (
            gpa_lookup.groupby('school_id')['school_avg_admitted_gpa']
            .first()
            .to_dict()
        )
        logger.info(f"school_avg_admitted_gpa lookup: {len(school_avg_admitted_gpa)} schools")

    # Save model and configuration
    save_model_and_config(
        model, calibrator, category_mappings, feature_names, categorical_indices,
        args.model_dir,
        school_avg_admitted_gpa=school_avg_admitted_gpa,
        z_stats=z_stats,
        target_encoding_map=target_encoding_map,
        target_encoding_global_mean=target_encoding_global_mean,
        major_encoding_map=major_encoding_map,
        major_encoding_global_mean=major_encoding_global_mean,
        model_type=args.model_type,
    )

    logger.info("Training pipeline complete!")


if __name__ == '__main__':
    main()
