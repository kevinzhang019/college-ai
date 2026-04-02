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
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import (
    auc, brier_score_loss, classification_report, confusion_matrix,
    log_loss, roc_auc_score,
)
from sklearn.model_selection import cross_val_score, StratifiedKFold, train_test_split
from sklearn.preprocessing import LabelEncoder

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Feature definitions
NUMERIC_FEATURES = [
    'gpa', 'sat_score', 'acceptance_rate', 'sat_25', 'sat_75',
    'act_25', 'act_75', 'enrollment', 'retention_rate', 'graduation_rate',
    'student_faculty_ratio', 'tuition_in_state', 'tuition_out_of_state',
    'pct_white', 'pct_black', 'pct_hispanic', 'pct_asian', 'pct_first_gen',
    'sat_percentile_at_school', 'gpa_vs_expected', 'median_earnings_10yr',
    'yield_rate',
    # New engineered features
    'sat_zscore_at_school', 'gpa_x_acceptance', 'sat_percentile_sq',
    'selectivity_x_sat', 'academic_composite_z', 'competitiveness_index',
]

CATEGORICAL_FEATURES = [
    'ownership', 'selectivity_bucket'
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

    Returns:
        df: preprocessed dataframe
        label_encoders: dictionary of LabelEncoder objects for categoricals
    """
    logger.info("Preprocessing data...")
    df = df.copy()

    # Store original indices for reference
    df_indices = df.index.copy()

    # Handle missing values in numeric features
    for col in NUMERIC_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna(df[col].median())

    # Handle missing values in categorical features
    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna('unknown')

    # Label encode categorical features for LightGBM
    label_encoders: Dict[str, LabelEncoder] = {}
    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            le = LabelEncoder()
            df[col] = le.fit_transform(df[col].astype(str))
            label_encoders[col] = le

    logger.info("Data preprocessing complete")
    return df, label_encoders


def prepare_features(
    df: pd.DataFrame,
    label_encoders: Dict[str, LabelEncoder]
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
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    """
    Split data into train, validation, and test sets.

    Returns:
        X_train, X_val, X_test, y_train, y_val, y_test
    """
    logger.info("Splitting data (60/20/20 train/val/test)...")

    # First split: 80% train, 20% test
    X_train, X_test, y_train, y_test = train_test_split(
        X, y,
        test_size=test_size,
        random_state=random_state,
        stratify=stratify_col,
    )

    # Split training set into train and validation (75/25 of train = 60/20 of total)
    stratify_train = y_train
    X_train, X_val, y_train, y_val = train_test_split(
        X_train, y_train,
        test_size=0.25,
        random_state=random_state,
        stratify=stratify_train,
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
        'max_bin': 127,
        'feature_pre_filter': True,
        'force_col_wise': True,
        'num_threads': os.cpu_count() or 4,
        'is_unbalance': is_unbalanced,
        'random_state': random_state,
        'verbose': -1,
    }

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
        'reg_alpha': 1e-5,
        'reg_lambda': 1e-5,
        'min_gain_to_split': 0.01,
        'bagging_freq': 5,
        'max_bin': 127,
        'feature_pre_filter': True,
        'force_col_wise': True,
        'num_threads': os.cpu_count() or 4,
        'is_unbalance': is_unbalanced,
        'random_state': 42,
        'verbose': -1,
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
        'max_bin': 127,
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
    return model


class LGBWrapper:
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
    model: lgb.Booster,
    X_cal: pd.DataFrame,
    y_cal: pd.Series,
) -> CalibratedClassifierCV:
    """Apply isotonic calibration using a held-out calibration set.

    Isotonic regression is preferred over Platt scaling (sigmoid) for gradient
    boosted models because GBMs often produce non-linear probability distortions
    that isotonic regression can capture more flexibly.
    """
    logger.info("Calibrating model with isotonic regression...")

    wrapper = LGBWrapper(model)

    calibrator = CalibratedClassifierCV(
        estimator=FrozenEstimator(wrapper),
        method='isotonic',
    )
    calibrator.fit(X_cal, y_cal)

    logger.info("Model calibration complete")
    return calibrator


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

    logger.info(f"AUC-ROC:   {auc_roc:.4f}")
    logger.info(f"Brier:     {brier:.4f}")
    logger.info(f"Log Loss:  {logloss:.4f}")
    logger.info(f"ECE:       {ece:.4f}")

    # Per-selectivity-bucket Brier scores
    if selectivity_buckets is not None:
        logger.info("\nPer-selectivity-bucket Brier scores:")
        for bucket in sorted(selectivity_buckets.unique()):
            mask = selectivity_buckets == bucket
            if mask.sum() > 0:
                bucket_brier = brier_score_loss(y_test[mask], y_pred_proba[mask])
                logger.info(f"  {bucket:15s}: {bucket_brier:.4f}  (n={mask.sum()})")

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
) -> Tuple[pd.Series, Dict[int, float], float]:
    """Bayesian target encoding for school_id with CV to prevent leakage.

    For each fold, the encoding is computed from the other folds only.
    Returns the encoded column, the full encoding map, and global mean.
    """
    global_mean = y.mean()
    encoded = pd.Series(np.nan, index=school_ids.index, dtype=float)

    kf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=random_state)
    for train_idx, val_idx in kf.split(school_ids, y):
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
    model: lgb.Booster,
    calibrator: Optional[CalibratedClassifierCV],
    label_encoders: Dict[str, Any],
    feature_names: List[str],
    categorical_indices: List[int],
    model_dir: str,
    school_avg_admitted_gpa: Optional[Dict[int, float]] = None,
    z_stats: Optional[Dict[str, float]] = None,
    target_encoding_map: Optional[Dict[int, float]] = None,
    target_encoding_global_mean: Optional[float] = None,
) -> None:
    """Save model, calibrator, label encoders, and configuration."""
    logger.info("Saving model and configuration...")

    # Create model directory if it doesn't exist
    Path(model_dir).mkdir(parents=True, exist_ok=True)

    # Save model, calibrator, label encoders, and school avg GPA lookup
    model_path = os.path.join(model_dir, 'admissions_lgbm.pkl')
    joblib.dump({
        'model': model,
        'calibrator': calibrator,
        'label_encoders': label_encoders,
        'school_avg_admitted_gpa': school_avg_admitted_gpa or {},
        'z_stats': z_stats,
        'target_encoding_map': target_encoding_map,
        'target_encoding_global_mean': target_encoding_global_mean,
    }, model_path)
    logger.info(f"Model saved to {model_path}")

    # Save configuration
    config = {
        'feature_names': feature_names,
        'categorical_indices': categorical_indices,
        'numeric_features': NUMERIC_FEATURES,
        'categorical_features': CATEGORICAL_FEATURES,
        'target': TARGET
    }

    config_path = os.path.join(model_dir, 'model_config.json')
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    logger.info(f"Configuration saved to {config_path}")


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
        default='preference_scraper/data/training_data.parquet',
        help='Path to training data parquet file'
    )
    parser.add_argument(
        '--model-dir',
        type=str,
        default='preference_scraper/admissions/model',
        help='Directory to save model and config'
    )
    parser.add_argument(
        '--n-trials',
        type=int,
        default=50,
        help='Number of Optuna trials for hyperparameter tuning'
    )
    parser.add_argument(
        '--no-imbalance-correction',
        action='store_true',
        help='Disable is_unbalance flag (may improve probability calibration)'
    )

    args = parser.parse_args()

    logger.info("Starting college admissions model training pipeline")
    logger.info(f"Data path: {args.data_path}")
    logger.info(f"Model directory: {args.model_dir}")

    # Load and preprocess data
    df = load_data(args.data_path)

    # --- Target encoding for school_id (before preprocessing drops it) ---
    target_encoding_map = None
    target_encoding_global_mean = None
    if 'school_id' in df.columns and TARGET in df.columns:
        school_ids = df['school_id']
        y_for_te = df[TARGET].astype(int)
        te_encoded, target_encoding_map, target_encoding_global_mean = compute_target_encoding(
            school_ids, y_for_te
        )
        df['school_target_encoded'] = te_encoded

    df, label_encoders = preprocess_data(df)

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

    # Prepare features and target
    X, categorical_indices, feature_names = prepare_features(df, label_encoders)
    y = df[TARGET].astype(int)

    # Save selectivity bucket for per-bucket evaluation
    selectivity_buckets = None
    if 'selectivity_bucket' in df.columns:
        # selectivity_bucket is label-encoded at this point; recover original from label encoder
        if 'selectivity_bucket' in label_encoders:
            le = label_encoders['selectivity_bucket']
            selectivity_buckets = pd.Series(
                le.inverse_transform(df['selectivity_bucket'].values),
                index=df.index,
            )

    # Split data
    stratify_col = selectivity_buckets if selectivity_buckets is not None else y
    X_train, X_val, X_test, y_train, y_val, y_test = split_data(
        X, y, stratify_col=stratify_col
    )

    # Align selectivity buckets with test set
    test_buckets = selectivity_buckets.loc[y_test.index] if selectivity_buckets is not None else None

    # Use half of validation set for calibration
    cal_split = len(X_val) // 2
    X_cal, X_val = X_val.iloc[:cal_split], X_val.iloc[cal_split:]
    y_cal, y_val = y_val.iloc[:cal_split], y_val.iloc[cal_split:]

    # Detect class imbalance
    pos_rate = y_train.mean()
    if args.no_imbalance_correction:
        is_unbalanced = False
        logger.info(f"Imbalance correction disabled (positive rate: {pos_rate:.1%})")
    else:
        is_unbalanced = pos_rate < 0.3 or pos_rate > 0.7
        if is_unbalanced:
            logger.info(f"Class imbalance detected (positive rate: {pos_rate:.1%}), enabling is_unbalance")

    # Hyperparameter tuning or use defaults
    if args.skip_tuning:
        logger.info("Using default hyperparameters (skip-tuning flag set)")
        best_params = get_default_params(is_unbalanced=is_unbalanced)
    else:
        best_params = tune_hyperparameters(
            X_train, y_train, categorical_indices, feature_names,
            n_trials=args.n_trials, is_unbalanced=is_unbalanced,
        )

    # Train model
    model = train_model(
        X_train, y_train, X_val, y_val,
        categorical_indices, feature_names, best_params,
        is_unbalanced=is_unbalanced,
    )

    # Calibrate model
    calibrator = calibrate_model(model, X_cal, y_cal)

    # Evaluate model
    evaluate_model(model, X_test, y_test, selectivity_buckets=test_buckets)

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
        model, calibrator, label_encoders, feature_names, categorical_indices,
        args.model_dir,
        school_avg_admitted_gpa=school_avg_admitted_gpa,
        z_stats=z_stats,
        target_encoding_map=target_encoding_map,
        target_encoding_global_mean=target_encoding_global_mean,
    )

    logger.info("Training pipeline complete!")


if __name__ == '__main__':
    main()
