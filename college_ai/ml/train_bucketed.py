"""Bucketed LightGBM training: trains a separate model per selectivity bucket.

Each bucket gets its own hyperparameter regime, calibration strategy,
per-bucket target encodings, and advanced training techniques (DART,
focal loss, monotone constraints, interaction constraints, sample weights).

Usage:
    python -m college_ai.ml.train_bucketed [--skip-tuning]
    python -m college_ai.ml.train_bucketed --bucket reach --skip-tuning
"""

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
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from college_ai.ml.train import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    TARGET,
    LGBWrapper,
    calibrate_model,
    compute_target_encoding,
    evaluate_model,
    generate_shap_summary,
    load_data,
    preprocess_data,
    split_data,
    train_model,
    tune_hyperparameters,
    _expected_calibration_error,
)
from college_ai.ml.bucket_configs import (
    BUCKET_CALIBRATION_METHOD,
    BUCKET_DEFAULT_PARAMS,
    BUCKET_ORDER,
    BUCKET_TUNING_PARAMS,
    BUCKET_USE_FOCAL_LOSS,
    BUCKET_USE_SAMPLE_WEIGHTS,
    BUCKET_USE_INTERACTION_CONSTRAINTS,
    build_monotone_constraints,
    build_interaction_constraints,
    focal_loss_objective,
    focal_loss_eval,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class FocalLGBWrapper:
    """Wraps a focal-loss LightGBM Booster to output probabilities.

    Focal loss boosters output raw log-odds; this applies sigmoid to convert
    to probabilities.  Also duck-types num_trees() for evaluate_model().
    """

    def __init__(self, booster):
        # type: (lgb.Booster) -> None
        self.booster = booster

    def predict(self, X, **kwargs):
        raw = self.booster.predict(X, **kwargs)
        return 1.0 / (1.0 + np.exp(-raw))

    def num_trees(self):
        return self.booster.num_trees()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prepare_bucket_features(
    df_bucket,          # type: pd.DataFrame
    category_mappings,  # type: Dict[str, Any]
    extra_numeric=None,  # type: Optional[List[str]]
):
    # type: (...) -> Tuple[pd.DataFrame, List[int], List[str]]
    """Build the feature matrix for a single bucket.

    Drops `selectivity_bucket` (constant within a bucket → zero variance).
    extra_numeric: additional numeric columns (e.g. target-encoded) to include
    without mutating the module-level NUMERIC_FEATURES list.
    """
    bucket_numeric = [f for f in NUMERIC_FEATURES if f in df_bucket.columns]
    # Append extra numeric features (target encodings) without mutating shared list
    for col in (extra_numeric or []):
        if col in df_bucket.columns and col not in bucket_numeric:
            bucket_numeric.append(col)
    bucket_categorical = [
        f for f in CATEGORICAL_FEATURES
        if f in df_bucket.columns and f != "selectivity_bucket"
    ]

    feature_cols = bucket_numeric + bucket_categorical
    X = df_bucket[feature_cols].copy()

    categorical_indices = [
        X.columns.get_loc(col) for col in bucket_categorical
    ]

    feature_names = X.columns.tolist()
    logger.info(
        f"  Features: {len(feature_names)} total "
        f"({len(bucket_numeric)} numeric, {len(bucket_categorical)} categorical)"
    )
    return X, categorical_indices, feature_names


def _compute_sample_weights(df_bucket):
    # type: (pd.DataFrame) -> Optional[np.ndarray]
    """Compute inverse-sqrt-frequency sample weights by school.

    Prevents large schools from dominating gradient signal.
    """
    if "school_id" not in df_bucket.columns:
        return None
    school_counts = df_bucket["school_id"].value_counts()
    weights = df_bucket["school_id"].map(
        lambda sid: 1.0 / np.sqrt(school_counts.get(sid, 1))
    ).values
    # Normalize so mean weight = 1
    weights = weights / weights.mean()
    return weights


def _train_single_bucket(
    bucket_name,        # type: str
    df_bucket,          # type: pd.DataFrame
    category_mappings,  # type: Dict[str, Any]
    skip_tuning,        # type: bool
    n_trials,           # type: int
    model_dir,          # type: str
    tune_brier=False,   # type: bool
    multi_objective=False,  # type: bool
):
    # type: (...) -> Dict[str, Any]
    """Train, calibrate, evaluate, and save one bucket's model."""
    bucket_dir = os.path.join(model_dir, "bucketed", bucket_name)
    Path(bucket_dir).mkdir(parents=True, exist_ok=True)

    logger.info(f"\n{'='*60}")
    logger.info(f"BUCKET: {bucket_name.upper()}  ({len(df_bucket)} rows)")
    logger.info(f"{'='*60}")

    pos_rate = df_bucket[TARGET].mean()
    logger.info(f"  Positive rate (admitted): {pos_rate:.1%}")

    # --- Per-bucket target encoding for school_id ---
    # Use StratifiedGroupKFold with groups=school_id to prevent leakage
    te_map = None   # type: Optional[Dict[int, float]]
    te_global_mean = None   # type: Optional[float]
    if "school_id" in df_bucket.columns:
        bucket_school_ids = df_bucket["school_id"]
        te_encoded, te_map, te_global_mean = compute_target_encoding(
            bucket_school_ids,
            df_bucket[TARGET].astype(int),
            smoothing=300,
            groups=bucket_school_ids,
        )
        df_bucket = df_bucket.copy()
        df_bucket["school_target_encoded"] = te_encoded

    # --- Per-bucket target encoding for major ---
    major_te_map = None   # type: Optional[Dict[str, float]]
    major_te_global_mean = None   # type: Optional[float]
    if "major" in df_bucket.columns:
        major_notna = df_bucket["major"].notna()
        if major_notna.any():
            major_groups = bucket_school_ids.loc[major_notna] if "school_id" in df_bucket.columns else None
            te_major, major_te_map, major_te_global_mean = compute_target_encoding(
                df_bucket.loc[major_notna, "major"],
                df_bucket.loc[major_notna, TARGET].astype(int),
                smoothing=100,
                groups=major_groups,
            )
            df_bucket = df_bucket.copy()
            df_bucket["major_target_encoded"] = float("nan")
            df_bucket.loc[major_notna, "major_target_encoded"] = te_major

    # Collect extra numeric columns (target encodings) without mutating shared list
    extra_numeric = []   # type: List[str]
    if "school_target_encoded" in df_bucket.columns:
        extra_numeric.append("school_target_encoded")
    if "major_target_encoded" in df_bucket.columns:
        extra_numeric.append("major_target_encoded")

    # Prepare features (drops selectivity_bucket)
    X, categorical_indices, feature_names = _prepare_bucket_features(
        df_bucket, category_mappings, extra_numeric=extra_numeric,
    )
    y = df_bucket[TARGET].astype(int)

    # --- Monotone constraints ---
    mc = build_monotone_constraints(feature_names)
    mc_active = sum(1 for c in mc if c != 0)
    logger.info(f"  Monotone constraints: {mc_active} features constrained")

    # --- Interaction constraints ---
    ic = None   # type: Optional[List[List[int]]]
    if BUCKET_USE_INTERACTION_CONSTRAINTS.get(bucket_name, False):
        ic = build_interaction_constraints(feature_names)
        logger.info(f"  Interaction constraint groups: {len(ic)}")

    # --- Sample weights ---
    sample_weights = None   # type: Optional[np.ndarray]
    if BUCKET_USE_SAMPLE_WEIGHTS.get(bucket_name, False):
        sample_weights = _compute_sample_weights(df_bucket)
        if sample_weights is not None:
            logger.info(
                f"  Sample weights: min={sample_weights.min():.2f}, "
                f"max={sample_weights.max():.2f}, mean={sample_weights.mean():.2f}"
            )

    # Split 60/20/20 — group by school_id to prevent school-level leakage
    bucket_groups = df_bucket["school_id"] if "school_id" in df_bucket.columns else None
    X_train, X_val, X_test, y_train, y_val, y_test = split_data(
        X, y, stratify_col=y, groups=bucket_groups,
    )

    # Align sample weights to train split
    train_weights = None
    if sample_weights is not None:
        # sample_weights is aligned to df_bucket index
        train_mask = X_train.index
        train_weights = sample_weights[
            np.isin(df_bucket.index.values, train_mask.values)
        ]
        # Fallback: just slice by position since split preserves relative order
        if len(train_weights) != len(X_train):
            # Re-compute for the train subset
            train_weights = _compute_sample_weights(
                df_bucket.loc[X_train.index]
            )

    # Use half of validation for calibration
    cal_split = len(X_val) // 2
    X_cal, X_val_es = X_val.iloc[:cal_split], X_val.iloc[cal_split:]
    y_cal, y_val_es = y_val.iloc[:cal_split], y_val.iloc[cal_split:]

    # Hyperparameters
    defaults = BUCKET_DEFAULT_PARAMS[bucket_name]
    is_unbalanced = defaults.get("is_unbalance", False)
    use_focal = BUCKET_USE_FOCAL_LOSS.get(bucket_name, False)
    is_dart = defaults.get("boosting_type") == "dart"

    if skip_tuning:
        logger.info("  Using default hyperparameters (skip-tuning)")
        best_params = dict(defaults)
    elif tune_brier or multi_objective:
        from college_ai.ml.train import tune_hyperparameters_brier
        train_groups = df_bucket.loc[X_train.index, "school_id"] if "school_id" in df_bucket.columns else None
        best_params = tune_hyperparameters_brier(
            X_train, y_train, categorical_indices, feature_names,
            n_trials=n_trials,
            is_unbalanced=is_unbalanced,
            monotone_constraints=mc,
            groups=train_groups,
            multi_objective=multi_objective,
        )
    else:
        best_params = tune_hyperparameters(
            X_train, y_train, categorical_indices, feature_names,
            n_trials=n_trials,
            is_unbalanced=is_unbalanced,
        )

    # Add monotone constraints to params
    best_params["monotone_constraints"] = mc
    best_params["monotone_constraints_method"] = "intermediate"

    # Add interaction constraints if enabled
    if ic is not None:
        best_params["interaction_constraints"] = ic

    # --- Train ---
    if use_focal:
        logger.info("  Using focal loss objective")
        model = _train_with_focal_loss(
            X_train, y_train, X_val_es, y_val_es,
            categorical_indices, feature_names, best_params,
            train_weights=train_weights,
            is_dart=is_dart,
        )
    else:
        model = _train_with_weights(
            X_train, y_train, X_val_es, y_val_es,
            categorical_indices, feature_names, best_params,
            is_unbalanced=is_unbalanced,
            train_weights=train_weights,
            is_dart=is_dart,
        )

    # For focal loss models, the booster outputs raw log-odds.
    # We wrap it in a FocalLGBWrapper for calibration/evaluation, but
    # save the raw booster (unwrapped) to the pickle.
    if use_focal:
        wrapper = FocalLGBWrapper(model)
        cal_method = BUCKET_CALIBRATION_METHOD[bucket_name]
        calibrator = calibrate_model(wrapper, X_cal, y_cal, method=cal_method)
        evaluate_model(wrapper, X_test, y_test)
    else:
        cal_method = BUCKET_CALIBRATION_METHOD[bucket_name]
        calibrator = calibrate_model(model, X_cal, y_cal, method=cal_method)
        evaluate_model(model, X_test, y_test)

    # SHAP summary
    shap_path = os.path.join(bucket_dir, "shap_summary.png")
    try:
        generate_shap_summary(
            model, X_test.head(100), feature_names, shap_path,
        )
    except Exception as e:
        logger.warning(f"  SHAP generation failed: {e}")

    # Save bucket artifacts (always save the raw booster, not the wrapper)
    model_path = os.path.join(bucket_dir, "model.pkl")
    joblib.dump({
        "model": model,
        "calibrator": calibrator,
        "category_mappings": category_mappings,
        "target_encoding_map": te_map,
        "target_encoding_global_mean": te_global_mean,
        "major_encoding_map": major_te_map,
        "major_encoding_global_mean": major_te_global_mean,
        "is_focal_loss": use_focal,
    }, model_path)

    config_path = os.path.join(bucket_dir, "config.json")
    with open(config_path, "w") as f:
        json.dump({
            "feature_names": feature_names,
            "categorical_indices": categorical_indices,
            "numeric_features": [
                f for f in NUMERIC_FEATURES if f in feature_names
            ],
            "categorical_features": [
                f for f in CATEGORICAL_FEATURES
                if f in feature_names and f != "selectivity_bucket"
            ],
            "target": TARGET,
            "calibration_method": cal_method,
            "boosting_type": defaults.get("boosting_type", "gbdt"),
            "uses_focal_loss": use_focal,
            "uses_sample_weights": BUCKET_USE_SAMPLE_WEIGHTS.get(bucket_name, False),
            "monotone_constrained_features": mc_active,
            "n_train": len(X_train),
            "n_test": len(X_test),
            "positive_rate": float(pos_rate),
        }, f, indent=2)

    logger.info(f"  Saved bucket model to {bucket_dir}")

    # Return test-set predictions for global evaluation
    if calibrator:
        y_prob = calibrator.predict_proba(X_test)[:, 1]
    elif use_focal:
        y_prob = FocalLGBWrapper(model).predict(X_test)
    else:
        y_prob = model.predict(X_test)

    return {
        "bucket": bucket_name,
        "y_test": y_test,
        "y_prob": y_prob,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "positive_rate": float(pos_rate),
    }


# ---------------------------------------------------------------------------
# Training variants
# ---------------------------------------------------------------------------

def _train_with_weights(
    X_train, y_train, X_val, y_val,
    categorical_indices, feature_names, params,
    is_unbalanced=False, train_weights=None, is_dart=False,
):
    # type: (...) -> lgb.Booster
    """Standard LightGBM training with optional sample weights."""
    logger.info("  Training model...")

    full_params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "max_bin": 127,
        "cat_smooth": 10,
        "min_data_per_group": 50,
        "feature_pre_filter": True,
        "force_col_wise": True,
        "num_threads": os.cpu_count() or 4,
        "is_unbalance": is_unbalanced,
        "verbose": -1,
        **params,
    }

    train_data = lgb.Dataset(
        X_train, label=y_train, weight=train_weights,
        categorical_feature=categorical_indices,
        feature_name=feature_names, free_raw_data=False,
    )
    val_data = lgb.Dataset(
        X_val, label=y_val, reference=train_data,
        categorical_feature=categorical_indices,
        feature_name=feature_names, free_raw_data=False,
    )

    # DART doesn't support early stopping — use fixed num_boost_round
    if is_dart:
        num_rounds = 300
        callbacks = [lgb.log_evaluation(period=100)]
    else:
        num_rounds = 2000
        callbacks = [
            lgb.early_stopping(stopping_rounds=50, verbose=True),
            lgb.log_evaluation(period=100),
        ]

    model = lgb.train(
        full_params, train_data, num_boost_round=num_rounds,
        valid_sets=[train_data, val_data], valid_names=["train", "valid"],
        callbacks=callbacks,
    )

    logger.info(f"  Model trained with {model.num_trees()} trees")
    return model


def _train_with_focal_loss(
    X_train, y_train, X_val, y_val,
    categorical_indices, feature_names, params,
    train_weights=None, is_dart=False,
):
    # type: (...) -> lgb.Booster
    """Train with custom focal loss objective."""
    logger.info("  Training with focal loss...")

    full_params = {
        "learning_rate": 0.03,
        "max_bin": 127,
        "cat_smooth": 10,
        "min_data_per_group": 50,
        "feature_pre_filter": True,
        "force_col_wise": True,
        "num_threads": os.cpu_count() or 4,
        "verbose": -1,
        **params,
    }
    # Set custom objective function — remove built-in objective/metric
    full_params.pop("objective", None)
    full_params.pop("metric", None)
    full_params.pop("is_unbalance", None)
    full_params["objective"] = focal_loss_objective

    # Compute init_score (log-odds of positive rate) for stable starting point
    pos_rate = y_train.mean()
    init_score = np.full(len(y_train), np.log(pos_rate / (1 - pos_rate)))
    init_score_val = np.full(len(y_val), np.log(pos_rate / (1 - pos_rate)))

    train_data = lgb.Dataset(
        X_train, label=y_train, weight=train_weights,
        init_score=init_score,
        categorical_feature=categorical_indices,
        feature_name=feature_names, free_raw_data=False,
    )
    val_data = lgb.Dataset(
        X_val, label=y_val, init_score=init_score_val,
        reference=train_data,
        categorical_feature=categorical_indices,
        feature_name=feature_names, free_raw_data=False,
    )

    if is_dart:
        num_rounds = 300
        callbacks = [lgb.log_evaluation(period=100)]
    else:
        num_rounds = 2000
        callbacks = [
            lgb.early_stopping(stopping_rounds=50, verbose=True),
            lgb.log_evaluation(period=100),
        ]

    model = lgb.train(
        full_params, train_data, num_boost_round=num_rounds,
        feval=focal_loss_eval,
        valid_sets=[train_data, val_data], valid_names=["train", "valid"],
        callbacks=callbacks,
    )

    logger.info(f"  Model trained with {model.num_trees()} trees (focal loss)")
    return model


# ---------------------------------------------------------------------------
# Global evaluation
# ---------------------------------------------------------------------------

def _global_evaluation(bucket_results):
    # type: (List[Dict[str, Any]]) -> None
    """Combine per-bucket test predictions and compute global metrics."""
    logger.info(f"\n{'='*60}")
    logger.info("GLOBAL EVALUATION (all buckets combined)")
    logger.info(f"{'='*60}")

    y_all = pd.concat([r["y_test"] for r in bucket_results])
    prob_all = np.concatenate([r["y_prob"] for r in bucket_results])

    auc_roc = roc_auc_score(y_all, prob_all)
    brier = brier_score_loss(y_all, prob_all)
    logloss = log_loss(y_all, prob_all)
    ece = _expected_calibration_error(y_all.values, prob_all)
    base_rate = y_all.mean()
    brier_clim = base_rate * (1 - base_rate)
    bss = 1 - (brier / brier_clim) if brier_clim > 0 else 0.0

    logger.info(f"AUC-ROC:   {auc_roc:.4f}")
    logger.info(f"Brier:     {brier:.4f}")
    logger.info(f"BSS:       {bss:.4f}")
    logger.info(f"Log Loss:  {logloss:.4f}")
    logger.info(f"ECE:       {ece:.4f}")
    logger.info(f"Total test samples: {len(y_all)}")

    logger.info("\nPer-bucket summary:")
    for r in bucket_results:
        bucket_auc = roc_auc_score(r["y_test"], r["y_prob"])
        bucket_brier = brier_score_loss(r["y_test"], r["y_prob"])
        bucket_base = r["y_test"].mean()
        bucket_clim = bucket_base * (1 - bucket_base)
        bucket_bss = 1 - (bucket_brier / bucket_clim) if bucket_clim > 0 else 0.0
        logger.info(
            f"  {r['bucket']:15s}: AUC={bucket_auc:.4f}  Brier={bucket_brier:.4f}  "
            f"BSS={bucket_bss:.4f}  n_test={r['n_test']}  pos_rate={r['positive_rate']:.1%}"
        )
    logger.info("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # type: () -> None
    parser = argparse.ArgumentParser(
        description="Train per-selectivity-bucket LightGBM models"
    )
    parser.add_argument(
        "--skip-tuning", action="store_true",
        help="Use default hyperparameters (skip Optuna tuning)",
    )
    parser.add_argument(
        "--data-path", type=str,
        default="data/training_data.parquet",
    )
    parser.add_argument(
        "--model-dir", type=str,
        default="model",
    )
    parser.add_argument(
        "--n-trials", type=int, default=50,
        help="Optuna trials per bucket",
    )
    parser.add_argument(
        "--bucket", type=str, default=None,
        choices=BUCKET_ORDER,
        help="Train only a single bucket (default: all)",
    )
    parser.add_argument(
        "--tune-brier", action="store_true",
        help="Use custom Optuna tuning optimizing Brier score",
    )
    parser.add_argument(
        "--multi-objective", action="store_true",
        help="Use multi-objective Optuna (AUC + Brier) with NSGA-II",
    )

    args = parser.parse_args()
    logger.info("Starting bucketed admissions model training pipeline")

    # ------------------------------------------------------------------
    # 1. Load & preprocess full dataset
    # ------------------------------------------------------------------
    df = load_data(args.data_path)

    # Save original selectivity_bucket strings before integer encoding
    if "selectivity_bucket" not in df.columns:
        raise ValueError(
            "selectivity_bucket column missing — run data_pipeline export first"
        )
    bucket_labels = df["selectivity_bucket"].copy()

    # Preprocess (fills NaN numerics, encodes categoricals to int codes)
    df, category_mappings = preprocess_data(df)

    # Compute shared z_stats from full dataset
    z_stats = {
        "gpa_mean": df["gpa"].mean(),
        "gpa_std": df["gpa"].std(),
        "sat_mean": df["sat_score"].mean(),
        "sat_std": df["sat_score"].std(),
    }
    logger.info(f"Shared z_stats: {z_stats}")

    # Compute shared school_avg_admitted_gpa
    school_avg_admitted_gpa = None   # type: Optional[Dict[int, float]]
    if "school_avg_admitted_gpa" in df.columns:
        gpa_lookup = df.dropna(subset=["school_avg_admitted_gpa"])
        if not gpa_lookup.empty:
            school_avg_admitted_gpa = (
                gpa_lookup.groupby("school_id")["school_avg_admitted_gpa"]
                .first()
                .to_dict()
            )

    # ------------------------------------------------------------------
    # 2. Train each bucket
    # ------------------------------------------------------------------
    buckets_to_train = [args.bucket] if args.bucket else BUCKET_ORDER
    bucket_results = []   # type: List[Dict[str, Any]]

    for bucket_name in buckets_to_train:
        mask = bucket_labels == bucket_name
        if mask.sum() == 0:
            logger.warning(f"No data for bucket '{bucket_name}', skipping")
            continue

        df_bucket = df.loc[mask].copy()
        result = _train_single_bucket(
            bucket_name=bucket_name,
            df_bucket=df_bucket,
            category_mappings=category_mappings,
            skip_tuning=args.skip_tuning,
            n_trials=args.n_trials,
            model_dir=args.model_dir,
            tune_brier=args.tune_brier,
            multi_objective=args.multi_objective,
        )
        bucket_results.append(result)

    # ------------------------------------------------------------------
    # 3. Global evaluation
    # ------------------------------------------------------------------
    if len(bucket_results) > 1:
        _global_evaluation(bucket_results)

    # ------------------------------------------------------------------
    # 4. Save manifest (shared artifacts)
    # ------------------------------------------------------------------
    manifest_dir = os.path.join(args.model_dir, "bucketed")
    Path(manifest_dir).mkdir(parents=True, exist_ok=True)

    manifest = {
        "buckets": [r["bucket"] for r in bucket_results],
        "z_stats": z_stats,
        "school_avg_admitted_gpa": {
            str(k): v for k, v in (school_avg_admitted_gpa or {}).items()
        },
    }
    manifest_path = os.path.join(manifest_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    logger.info(f"Manifest saved to {manifest_path}")
    logger.info("Bucketed training pipeline complete!")


if __name__ == "__main__":
    main()
