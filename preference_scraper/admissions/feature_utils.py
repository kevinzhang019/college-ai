"""
Shared feature engineering utilities.

All engineered features are computed here so that data_pipeline.py (training)
and predict.py (inference) use identical logic — preventing train/serve skew.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Selectivity bucket
# ---------------------------------------------------------------------------

def selectivity_bucket(acceptance_rate: float | None) -> str:
    if acceptance_rate is None or (isinstance(acceptance_rate, float) and np.isnan(acceptance_rate)):
        return "unknown"
    if acceptance_rate < 0.15:
        return "reach"
    if acceptance_rate < 0.40:
        return "competitive"
    if acceptance_rate < 0.70:
        return "match"
    return "safety"


# ---------------------------------------------------------------------------
# Single-row feature computation (used by predict.py at inference time)
# ---------------------------------------------------------------------------

def compute_features_single(
    gpa: float,
    sat: float,
    acceptance_rate: float | None,
    sat_avg: float | None,
    sat_25: float | None,
    sat_75: float | None,
    graduation_rate: float | None,
    avg_admitted_gpa: float | None,
    z_stats: dict | None = None,
) -> dict:
    """Compute all engineered features for a single applicant.

    Args:
        gpa: Applicant GPA (capped at 4.0).
        sat: SAT score (400-1600).
        acceptance_rate: School acceptance rate (0-1).
        sat_avg: School average SAT.
        sat_25: School 25th percentile SAT.
        sat_75: School 75th percentile SAT.
        graduation_rate: School graduation rate (0-1).
        avg_admitted_gpa: Average GPA of admitted students at this school.
        z_stats: Dict with keys gpa_mean, gpa_std, sat_mean, sat_std from
                 training set.  Required for academic_composite_z.

    Returns:
        Dict of engineered feature values.
    """
    acc = acceptance_rate if acceptance_rate is not None else 0.5
    s25 = sat_25 or 0
    s75 = sat_75 or 0
    sat_range = s75 - s25

    # --- existing features ---
    sat_percentile = ((sat - s25) / sat_range) if sat_range > 0 else 0.5

    if avg_admitted_gpa is not None:
        gpa_vs_expected = gpa - avg_admitted_gpa
    else:
        gpa_vs_expected = gpa - (3.0 + acc * (-1.0))

    bucket = selectivity_bucket(acc)

    # --- new features ---
    # SAT z-score at school (IQR → stddev via / 1.35)
    iqr_std = sat_range / 1.35 if sat_range > 0 else None
    s_avg = sat_avg if sat_avg is not None else ((s25 + s75) / 2 if sat_range > 0 else None)
    if iqr_std and s_avg:
        sat_zscore = (sat - s_avg) / iqr_std
    else:
        sat_zscore = 0.0

    # GPA × acceptance rate
    gpa_x_acceptance = gpa * acc

    # Squared SAT percentile (nonlinear tail effect)
    sat_percentile_sq = sat_percentile ** 2

    # Selectivity × SAT percentile
    selectivity_x_sat = (1 - acc) * sat_percentile

    # Academic composite z-score
    if z_stats:
        z_gpa = (gpa - z_stats["gpa_mean"]) / z_stats["gpa_std"] if z_stats["gpa_std"] > 0 else 0.0
        z_sat = (sat - z_stats["sat_mean"]) / z_stats["sat_std"] if z_stats["sat_std"] > 0 else 0.0
        academic_composite_z = (z_gpa + z_sat) / 2
    else:
        academic_composite_z = 0.0

    # Competitiveness index
    grad = graduation_rate if graduation_rate is not None else 0.5
    s_avg_for_index = s_avg if s_avg else 1000
    competitiveness_index = (
        0.4 * (1 - acc)
        + 0.35 * (s_avg_for_index - 800) / 800
        + 0.25 * grad
    )

    return {
        "sat_percentile_at_school": np.clip(sat_percentile, -1, 2),
        "gpa_vs_expected": gpa_vs_expected,
        "selectivity_bucket": bucket,
        "sat_zscore_at_school": sat_zscore,
        "gpa_x_acceptance": gpa_x_acceptance,
        "sat_percentile_sq": sat_percentile_sq,
        "selectivity_x_sat": selectivity_x_sat,
        "academic_composite_z": academic_composite_z,
        "competitiveness_index": competitiveness_index,
    }


# ---------------------------------------------------------------------------
# DataFrame-level feature computation (used by data_pipeline.py for training)
# ---------------------------------------------------------------------------

def compute_features_df(
    df: pd.DataFrame,
    z_stats: dict | None = None,
) -> pd.DataFrame:
    """Compute all engineered features on a DataFrame of applicant rows.

    Expects columns: sat_score, sat_25, sat_75, sat_avg, gpa,
    acceptance_rate, graduation_rate, school_avg_admitted_gpa (may have NaNs).

    Args:
        df: DataFrame with raw applicant + school features.
        z_stats: If None, z-stats are computed from df itself (training mode).
                 Pass explicit stats at inference time to avoid data leakage.

    Returns:
        DataFrame with new feature columns added.
    """
    df = df.copy()

    # --- existing features ---
    sat_range = df["sat_75"] - df["sat_25"]
    df["sat_percentile_at_school"] = (
        (df["sat_score"] - df["sat_25"]) / sat_range.replace(0, float("nan"))
    ).clip(-1, 2)

    fallback = 3.0 + df["acceptance_rate"].fillna(0.5).clip(0, 1) * (-1.0)
    df["gpa_vs_expected"] = df["gpa"] - df["school_avg_admitted_gpa"].fillna(fallback)

    df["selectivity_bucket"] = df["acceptance_rate"].apply(selectivity_bucket)

    # --- new features ---
    acc = df["acceptance_rate"].fillna(0.5)

    # SAT z-score at school
    iqr_std = sat_range / 1.35
    s_avg = df["sat_avg"].fillna((df["sat_25"] + df["sat_75"]) / 2)
    df["sat_zscore_at_school"] = (
        (df["sat_score"] - s_avg) / iqr_std.replace(0, float("nan"))
    ).fillna(0.0)

    # GPA × acceptance rate
    df["gpa_x_acceptance"] = df["gpa"] * acc

    # Squared SAT percentile
    df["sat_percentile_sq"] = df["sat_percentile_at_school"] ** 2

    # Selectivity × SAT percentile
    df["selectivity_x_sat"] = (1 - acc) * df["sat_percentile_at_school"]

    # Academic composite z-score
    if z_stats is None:
        z_stats = {
            "gpa_mean": df["gpa"].mean(),
            "gpa_std": df["gpa"].std(),
            "sat_mean": df["sat_score"].mean(),
            "sat_std": df["sat_score"].std(),
        }
    gpa_std = z_stats["gpa_std"] if z_stats["gpa_std"] > 0 else 1.0
    sat_std = z_stats["sat_std"] if z_stats["sat_std"] > 0 else 1.0
    z_gpa = (df["gpa"] - z_stats["gpa_mean"]) / gpa_std
    z_sat = (df["sat_score"] - z_stats["sat_mean"]) / sat_std
    df["academic_composite_z"] = (z_gpa + z_sat) / 2

    # Competitiveness index
    grad = df["graduation_rate"].fillna(0.5)
    s_avg_idx = s_avg.fillna(1000)
    df["competitiveness_index"] = (
        0.4 * (1 - acc)
        + 0.35 * (s_avg_idx - 800) / 800
        + 0.25 * grad
    )

    return df, z_stats
