"""Per-selectivity-bucket hyperparameter configs for the bucketed model pipeline."""

from __future__ import annotations

import numpy as np
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Bucket definitions (must match feature_utils.selectivity_bucket())
# ---------------------------------------------------------------------------
BUCKET_ORDER = ["reach", "competitive", "match", "safety"]

BUCKET_THRESHOLDS = {
    "reach": (0.0, 0.15),
    "competitive": (0.15, 0.40),
    "match": (0.40, 0.70),
    "safety": (0.70, 1.01),
}

# ---------------------------------------------------------------------------
# Default LightGBM hyperparameters per bucket
# ---------------------------------------------------------------------------
# Reach   – 12K rows, ~1:1 balance, noisy holistic admissions → DART mode,
#           heavy regularisation, slow learning, shallow trees.
# Competitive – 13K rows, ~2.5:1 imbalance → DART mode, moderate complexity.
# Match   – 37K rows, ~5:1 imbalance → standard gbdt, most complex model.
# Safety  – 68K rows, ~15:1 imbalance → standard gbdt, sigmoid calibration.
# ---------------------------------------------------------------------------

_SHARED = {
    "metric": "binary_logloss",
    "max_bin": 127,
    "cat_smooth": 10,
    "min_data_per_group": 50,
    "feature_pre_filter": True,
    "force_col_wise": True,
    "verbose": -1,
}  # type: Dict[str, Any]


def _merge(overrides):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    return {**_SHARED, **overrides}


BUCKET_DEFAULT_PARAMS = {
    "reach": _merge({
        "objective": "binary",
        "boosting_type": "dart",
        "learning_rate": 0.03,
        "num_leaves": 31,
        "max_depth": 5,
        "min_child_samples": 50,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "feature_fraction_bynode": 0.9,   # was 0.8; combined with colsample=0.8 → 0.72 effective
        "reg_alpha": 0.1,
        "reg_lambda": 0.1,
        "min_gain_to_split": 0.02,
        "min_data_in_bin": 10,
        "bagging_freq": 5,
        "is_unbalance": False,
        "extra_trees": True,
        "monotone_penalty": 0.01,
        "path_smooth": 40,                # smooth leaf weights toward parent — prevents overfit on sparse subgroups
        "min_sum_hessian_in_leaf": 5,     # more principled than min_child_samples for imbalanced hessians
        "cat_smooth": 30,                 # increased smoothing for small noisy bucket
        "max_cat_threshold": 64,          # avoid truncating major split candidates
        # DART-specific
        "drop_rate": 0.1,
        "skip_drop": 0.5,
        "max_drop": 50,
    }),
    "competitive": _merge({
        "objective": "binary",
        "boosting_type": "dart",
        "learning_rate": 0.04,
        "num_leaves": 48,
        "max_depth": 6,
        "min_child_samples": 30,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "feature_fraction_bynode": 0.85,
        "reg_alpha": 0.05,
        "reg_lambda": 0.05,
        "min_gain_to_split": 0.01,
        "min_data_in_bin": 7,
        "bagging_freq": 5,
        "is_unbalance": False,
        "monotone_penalty": 0.005,
        "path_smooth": 20,
        "min_sum_hessian_in_leaf": 5,
        "cat_smooth": 15,
        "max_cat_threshold": 64,
        # DART-specific
        "drop_rate": 0.08,
        "skip_drop": 0.5,
        "max_drop": 50,
    }),
    "match": _merge({
        "objective": "binary",
        "boosting_type": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 64,
        "max_depth": 7,
        "min_child_samples": 20,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 0.05,              # was 1e-4 (essentially unregularized)
        "reg_lambda": 0.05,             # was 1e-4
        "min_gain_to_split": 0.01,
        "bagging_freq": 5,
        "is_unbalance": False,
        "path_smooth": 10,
        "min_sum_hessian_in_leaf": 10,
        "max_cat_threshold": 64,
        "linear_tree": True,            # Ridge regression in leaves — captures GPA/SAT linearity
        "linear_lambda": 20.0,          # Ridge regularization for leaf linear models
        "pos_bagging_fraction": 0.7,    # was missing — 5:1 imbalance needs class handling
        "neg_bagging_fraction": 1.0,
    }),
    "safety": _merge({
        "objective": "binary",
        "boosting_type": "gbdt",
        "learning_rate": 0.03,
        "num_leaves": 32,
        "max_depth": 5,
        "min_child_samples": 50,
        "subsample": 0.7,
        "colsample_bytree": 0.7,
        "reg_alpha": 0.05,
        "reg_lambda": 0.05,
        "min_gain_to_split": 0.001,
        "bagging_freq": 5,
        "is_unbalance": False,
        "path_smooth": 5,
        "min_sum_hessian_in_leaf": 20,   # 15:1 imbalance — raw counts are misleading
        "max_cat_threshold": 64,
        "linear_tree": True,
        "linear_lambda": 10.0,
        # Class-specific bagging — subsample the overwhelming positive class
        # instead of reweighting the loss (more targeted than is_unbalance)
        "pos_bagging_fraction": 0.5,
        "neg_bagging_fraction": 1.0,
    }),
}  # type: Dict[str, Dict[str, Any]]

# ---------------------------------------------------------------------------
# Calibration method per bucket
# ---------------------------------------------------------------------------
BUCKET_CALIBRATION_METHOD = {
    "reach": "isotonic",
    "competitive": "isotonic",
    "match": "venn_abers",       # Venn-ABERS for better calibration at 5:1 imbalance
    "safety": "venn_abers",      # Venn-ABERS for better calibration at 15:1 imbalance
}  # type: Dict[str, str]

# ---------------------------------------------------------------------------
# Use focal loss for reach bucket (noisy, borderline cases)
# ---------------------------------------------------------------------------
BUCKET_USE_FOCAL_LOSS = {
    "reach": True,
    "competitive": False,
    "match": False,
    "safety": False,
}  # type: Dict[str, bool]

FOCAL_LOSS_GAMMA = 2.0   # focus parameter — higher = more focus on hard examples
FOCAL_LOSS_ALPHA = 0.25  # class balance weight

# ---------------------------------------------------------------------------
# School-level inverse-frequency sample weighting
# ---------------------------------------------------------------------------
BUCKET_USE_SAMPLE_WEIGHTS = {
    "reach": False,    # too few schools, weighting could amplify noise
    "competitive": True,
    "match": True,
    "safety": True,
}  # type: Dict[str, bool]

# ---------------------------------------------------------------------------
# Monotone constraints
# ---------------------------------------------------------------------------
# Maps feature name → constraint direction.
# +1 = increasing (higher value → higher P(admit))
# -1 = decreasing (higher value → lower P(admit))
#  0 = unconstrained
#
# Applied to features where the relationship direction is known a priori.
# Uses "intermediate" method (less restrictive than "basic").
# ---------------------------------------------------------------------------

MONOTONE_FEATURE_CONSTRAINTS = {
    "gpa": 1,                          # higher GPA → more likely admitted
    "sat_score": 1,                    # higher SAT → more likely admitted
    "acceptance_rate": 1,              # higher acceptance rate → easier to get in
    "sat_percentile_at_school": 1,     # higher percentile → better fit
    "gpa_vs_expected": 1,              # above expected GPA → advantage
    "sat_zscore_at_school": 1,         # above average SAT → advantage
    "gpa_zscore_at_school": 1,         # above average GPA → advantage
    "academic_composite_z": 1,         # stronger overall academics → advantage
    "gpa_x_acceptance": 1,             # GPA × acceptance — both positive
    "sat_ratio": 1,                    # SAT / school_avg — higher is better
    "graduation_rate": 1,              # better school → typically higher standards but data correlates positively
    "retention_rate": 1,               # same reasoning
    "sat_above_75th": 1,              # above 75th pct → advantage
    "sat_below_25th": -1,             # below 25th pct → disadvantage
    "acceptance_rate_sq": 1,          # follows acceptance_rate monotonicity
    "academic_fit": 1,                 # higher school-relative fit → more likely admitted
}  # type: Dict[str, int]


def build_monotone_constraints(feature_names):
    # type: (List[str]) -> List[int]
    """Build monotone constraint vector aligned to feature_names list."""
    return [
        MONOTONE_FEATURE_CONSTRAINTS.get(f, 0) for f in feature_names
    ]


# ---------------------------------------------------------------------------
# Feature interaction constraints
# ---------------------------------------------------------------------------
# Group features that are allowed to interact within the same tree path.
# Features in different groups cannot appear in the same branch.
# This prevents learning spurious high-order interactions on small data.
#
# Only applied to reach and competitive (small datasets).
# ---------------------------------------------------------------------------

BUCKET_USE_INTERACTION_CONSTRAINTS = {
    "reach": True,
    "competitive": True,
    "match": False,
    "safety": False,
}  # type: Dict[str, bool]

# Feature groups for interaction constraints
INTERACTION_GROUPS = {
    "applicant_academic": [
        "gpa", "sat_score", "academic_composite_z",
        "sat_percentile_at_school", "gpa_vs_expected",
        "sat_zscore_at_school", "gpa_zscore_at_school",
        "sat_percentile_sq", "sat_excess", "gpa_excess", "sat_ratio",
        "academic_fit",
    ],
    "school_stats": [
        "acceptance_rate", "sat_25", "sat_75", "act_25", "act_75",
        "enrollment", "retention_rate", "graduation_rate",
        "student_faculty_ratio", "tuition_in_state", "tuition_out_of_state",
        "median_earnings_10yr", "yield_rate", "competitiveness_index",
        "log_enrollment", "log_earnings",
        "niche_academics_ord", "niche_value_ord", "niche_professors_ord",
        "niche_diversity_ord", "niche_campus_ord", "niche_overall_ord",
        "niche_rank", "avg_annual_cost", "cost_earnings_ratio",
        "holistic_signal", "sat_range",
    ],
    "fit_interactions": [
        "gpa_x_acceptance", "selectivity_x_sat",
        "gpa_x_competitiveness", "sat_x_competitiveness",
        "instate_x_public", "residency_x_acceptance",
        "stem_competitive_x_acceptance", "is_yield_protector",
        "yield_x_overqualification",
    ],
    "demographics": [
        "pct_white", "pct_black", "pct_hispanic", "pct_asian", "pct_first_gen",
    ],
}


def build_interaction_constraints(feature_names):
    # type: (List[str]) -> List[List[int]]
    """Build interaction constraint groups as list of index lists."""
    constraints = []
    for group_features in INTERACTION_GROUPS.values():
        indices = []
        for feat in group_features:
            if feat in feature_names:
                indices.append(feature_names.index(feat))
        if indices:
            constraints.append(indices)
    # Any features not in any group get their own singleton group
    covered = set()
    for group_features in INTERACTION_GROUPS.values():
        covered.update(group_features)
    for i, feat in enumerate(feature_names):
        if feat not in covered:
            constraints.append([i])
    return constraints


# ---------------------------------------------------------------------------
# Tuning overrides (used by LightGBMTunerCV)
# ---------------------------------------------------------------------------

BUCKET_TUNING_PARAMS = {
    "reach": {
        "learning_rate": 0.03,
        "is_unbalance": False,
        "nfold": 5,
        "num_boost_round": 800,
        "stopping_rounds": 40,
    },
    "competitive": {
        "learning_rate": 0.04,
        "is_unbalance": False,
        "nfold": 3,
        "num_boost_round": 1000,
        "stopping_rounds": 30,
    },
    "match": {
        "learning_rate": 0.05,
        "is_unbalance": False,
        "nfold": 3,
        "num_boost_round": 1000,
        "stopping_rounds": 30,
    },
    "safety": {
        "learning_rate": 0.03,
        "is_unbalance": False,
        "nfold": 3,
        "num_boost_round": 1000,
        "stopping_rounds": 30,
    },
}  # type: Dict[str, Dict[str, Any]]


# ---------------------------------------------------------------------------
# Focal loss implementation (custom objective for LightGBM)
# ---------------------------------------------------------------------------

def focal_loss_objective(y_pred, dtrain):
    """Focal loss gradient and hessian for LightGBM custom objective.

    Focal loss downweights easy examples and focuses learning on hard,
    borderline cases. Useful for the reach bucket where many examples
    are either clearly admitted or clearly rejected.

    y_pred is raw score (log-odds), not probability.
    """
    gamma = FOCAL_LOSS_GAMMA
    alpha = FOCAL_LOSS_ALPHA
    y_true = dtrain.get_label()

    # Convert log-odds to probability
    p = 1.0 / (1.0 + np.exp(-y_pred))
    p = np.clip(p, 1e-7, 1 - 1e-7)

    # Focal weights
    pt = np.where(y_true == 1, p, 1 - p)
    alpha_t = np.where(y_true == 1, alpha, 1 - alpha)
    focal_weight = alpha_t * (1 - pt) ** gamma

    # Gradient of cross-entropy: p - y
    ce_grad = p - y_true

    # Focal gradient (chain rule with focal weight)
    log_pt = np.where(y_true == 1, np.log(p), np.log(1 - p))
    grad = focal_weight * (
        gamma * (1 - pt) ** (gamma - 1) * pt * log_pt * (2 * y_true - 1)
        + (1 - pt) ** gamma * ce_grad
    )

    # Hessian approximation
    hess = focal_weight * (1 - pt) ** gamma * p * (1 - p)
    hess = np.maximum(hess, 1e-7)  # ensure positive

    return grad, hess


def focal_loss_eval(y_pred, dtrain):
    """Focal loss evaluation metric for LightGBM."""
    y_true = dtrain.get_label()
    p = 1.0 / (1.0 + np.exp(-y_pred))
    p = np.clip(p, 1e-7, 1 - 1e-7)

    pt = np.where(y_true == 1, p, 1 - p)
    alpha_t = np.where(y_true == 1, FOCAL_LOSS_ALPHA, 1 - FOCAL_LOSS_ALPHA)
    focal_loss = -alpha_t * (1 - pt) ** FOCAL_LOSS_GAMMA * np.log(pt)

    return "focal_loss", float(np.mean(focal_loss)), False
