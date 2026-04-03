"""
Shared feature engineering utilities.

All engineered features are computed here so that data_pipeline.py (training)
and predict.py (inference) use identical logic — preventing train/serve skew.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Selectivity bucket
# ---------------------------------------------------------------------------

def selectivity_bucket(acceptance_rate):
    # type: (Optional[float]) -> str
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
# Niche letter-grade → ordinal integer mapping
# ---------------------------------------------------------------------------

GRADE_ORD = {
    "A+": 12, "A": 11, "A-": 10,
    "B+": 9, "B": 8, "B-": 7,
    "C+": 6, "C": 5, "C-": 4,
    "D+": 3, "D": 2, "D-": 1,
    "F": 0,
}


def grade_to_ordinal(grade):
    # type: (Optional[str]) -> Optional[float]
    """Convert a Niche letter grade to ordinal integer (A+=12 … F=0)."""
    if grade is None or (isinstance(grade, float) and np.isnan(grade)):
        return None
    return GRADE_ORD.get(str(grade).strip(), None)


# ---------------------------------------------------------------------------
# Major tier mapping
# ---------------------------------------------------------------------------

MAJOR_TIER_MAP = {
    # stem_competitive — hardest to get into at the same school
    "Computer Science": "stem_competitive",
    "Engineering": "stem_competitive",
    "Nursing": "stem_competitive",
    "Data Science": "stem_competitive",
    # stem_standard
    "Biology": "stem_standard",
    "Chemistry": "stem_standard",
    "Physics": "stem_standard",
    "Mathematics": "stem_standard",
    "Environmental Science": "stem_standard",
    "Neuroscience": "stem_standard",
    "Biochemistry": "stem_standard",
    "Statistics": "stem_standard",
    "Astronomy": "stem_standard",
    # health
    "Health Professions": "health",
    "Public Health": "health",
    "Kinesiology and Physical Therapy": "health",
    "Pharmacy": "health",
    # business
    "Business and Management": "business",
    "Finance and Accounting": "business",
    "Economics": "business",
    "Marketing": "business",
    "Hospitality": "business",
    "Real Estate": "business",
    # arts_humanities
    "English": "arts_humanities",
    "History": "arts_humanities",
    "Philosophy": "arts_humanities",
    "Art": "arts_humanities",
    "Music": "arts_humanities",
    "Theater": "arts_humanities",
    "Film and Photography": "arts_humanities",
    "Foreign Languages": "arts_humanities",
    "Religious Studies": "arts_humanities",
    "Liberal Arts": "arts_humanities",
    "Performing Arts": "arts_humanities",
    "Communications": "arts_humanities",
    "Journalism": "arts_humanities",
    "Architecture": "arts_humanities",
    # social_science
    "Psychology": "social_science",
    "Political Science": "social_science",
    "Sociology": "social_science",
    "Anthropology": "social_science",
    "Social Work": "social_science",
    "International Relations": "social_science",
    "Gender Studies": "social_science",
    "Public Policy": "social_science",
    # education
    "Education": "education",
    # applied
    "Criminal Justice": "applied",
    "Agriculture": "applied",
    "Culinary Arts": "applied",
    "Information Technology": "applied",
    "Aviation": "applied",
}


def major_to_tier(major):
    # type: (Optional[str]) -> Optional[str]
    """Map a major string to its tier bucket."""
    if major is None or (isinstance(major, str) and major.strip() == ""):
        return None
    return MAJOR_TIER_MAP.get(major, "other")


# ---------------------------------------------------------------------------
# Known yield-protection schools
# ---------------------------------------------------------------------------

YIELD_PROTECTOR_IDS = {
    # school_ids for known yield-protecting schools (Tufts, Northeastern, etc.)
    # Populated at runtime from DB if not hardcoded.
}

YIELD_PROTECTOR_NAMES = {
    "tufts university", "northeastern university", "tulane university",
    "university of chicago", "emory university", "case western reserve university",
    "boston university", "brandeis university", "washington university in st. louis",
    "lehigh university", "rensselaer polytechnic institute",
    "university of rochester", "college of william and mary",
    "american university", "george washington university",
}


# ---------------------------------------------------------------------------
# Single-row feature computation (used by predict.py at inference time)
# ---------------------------------------------------------------------------

def compute_features_single(
    gpa,          # type: float
    sat,          # type: float
    acceptance_rate,  # type: Optional[float]
    sat_avg,      # type: Optional[float]
    sat_25,       # type: Optional[float]
    sat_75,       # type: Optional[float]
    graduation_rate,  # type: Optional[float]
    avg_admitted_gpa,  # type: Optional[float]
    z_stats=None,      # type: Optional[dict]
    residency=None,    # type: Optional[str]
    ownership=None,    # type: Optional[int]
    enrollment=None,   # type: Optional[int]
    median_earnings_10yr=None,  # type: Optional[float]
    niche_grades=None,  # type: Optional[dict]
    school_name=None,   # type: Optional[str]
    avg_annual_cost=None,  # type: Optional[float]
    niche_rank=None,    # type: Optional[int]
    yield_rate=None,    # type: Optional[float]
):
    # type: (...) -> dict
    """Compute all engineered features for a single applicant.

    Returns:
        Dict of engineered feature values.
    """
    acc = acceptance_rate if acceptance_rate is not None else 0.5
    s25 = sat_25 or 0
    s75 = sat_75 or 0
    sat_range = s75 - s25

    # --- SAT percentile at school ---
    sat_percentile = ((sat - s25) / sat_range) if sat_range > 0 else 0.5

    # --- GPA vs expected ---
    if avg_admitted_gpa is not None:
        gpa_vs_expected = gpa - avg_admitted_gpa
    else:
        gpa_vs_expected = gpa - (3.0 + acc * (-1.0))

    bucket = selectivity_bucket(acc)

    # --- SAT z-score at school ---
    iqr_std = sat_range / 1.35 if sat_range > 0 else None
    s_avg = sat_avg if sat_avg is not None else ((s25 + s75) / 2 if sat_range > 0 else None)
    if iqr_std and s_avg:
        sat_zscore = (sat - s_avg) / iqr_std
    else:
        sat_zscore = 0.0

    # --- GPA z-score at school (NEW) ---
    if avg_admitted_gpa is not None and avg_admitted_gpa > 0:
        # Approximate GPA std at school as 0.5 (typical spread)
        gpa_zscore_at_school = (gpa - avg_admitted_gpa) / 0.5
    else:
        gpa_zscore_at_school = 0.0

    # --- GPA × acceptance rate ---
    gpa_x_acceptance = gpa * acc

    # --- Squared SAT percentile ---
    sat_percentile_sq = sat_percentile ** 2

    # --- Selectivity × SAT percentile ---
    selectivity_x_sat = (1 - acc) * sat_percentile

    # --- Academic composite z-score ---
    if z_stats:
        z_gpa = (gpa - z_stats["gpa_mean"]) / z_stats["gpa_std"] if z_stats["gpa_std"] > 0 else 0.0
        z_sat = (sat - z_stats["sat_mean"]) / z_stats["sat_std"] if z_stats["sat_std"] > 0 else 0.0
        academic_composite_z = (z_gpa + z_sat) / 2
    else:
        academic_composite_z = 0.0

    # --- Competitiveness index ---
    grad = graduation_rate if graduation_rate is not None else 0.5
    s_avg_for_index = s_avg if s_avg else 1000
    competitiveness_index = (
        0.4 * (1 - acc)
        + 0.35 * (s_avg_for_index - 800) / 800
        + 0.25 * grad
    )

    # --- Competitiveness interactions ---
    gpa_x_competitiveness = gpa * competitiveness_index
    sat_x_competitiveness = sat_zscore * competitiveness_index

    # --- Residency interactions ---
    if residency is not None:
        is_instate = 1.0 if residency == "inState" else 0.0
        is_public = 1.0 if ownership == 1 else 0.0
        instate_x_public = is_instate * is_public
        residency_x_acceptance = is_instate * acc
    else:
        instate_x_public = float("nan")
        residency_x_acceptance = float("nan")

    # --- Overqualification features (NEW) ---
    sat_excess = max(0.0, sat - s75) if s75 > 0 else 0.0
    gpa_excess = max(0.0, gpa - (avg_admitted_gpa or 4.0))

    # --- SAT ratio (NEW) ---
    sat_ratio = (sat / s_avg) if s_avg and s_avg > 0 else 1.0

    # --- Log transforms (NEW) ---
    log_enrollment = np.log1p(enrollment) if enrollment is not None else None
    log_earnings = np.log1p(median_earnings_10yr) if median_earnings_10yr is not None else None

    # --- Yield protector flag (NEW) ---
    is_yield_protector = 0.0
    if school_name is not None:
        if school_name.lower().strip() in YIELD_PROTECTOR_NAMES:
            is_yield_protector = 1.0

    # --- Niche grade features (NEW) ---
    niche_academics_ord = None
    niche_value_ord = None
    niche_professors_ord = None
    niche_diversity_ord = None
    niche_campus_ord = None
    niche_overall_ord = None
    if niche_grades:
        niche_academics_ord = grade_to_ordinal(niche_grades.get("academics"))
        niche_value_ord = grade_to_ordinal(niche_grades.get("value"))
        niche_professors_ord = grade_to_ordinal(niche_grades.get("professors"))
        niche_diversity_ord = grade_to_ordinal(niche_grades.get("diversity"))
        niche_campus_ord = grade_to_ordinal(niche_grades.get("campus"))
        niche_overall_ord = grade_to_ordinal(niche_grades.get("overall_grade"))

    # --- Cost affordability (NEW) ---
    cost_earnings_ratio = None
    if avg_annual_cost is not None and median_earnings_10yr is not None and median_earnings_10yr > 0:
        cost_earnings_ratio = avg_annual_cost / median_earnings_10yr

    # --- Binary threshold features ---
    sat_above_75th = float(sat > s75) if s75 > 0 else 0.0
    sat_below_25th = float(sat < s25) if s25 > 0 else 0.0

    # --- Overqualification composite ---
    overqualification_index = (sat_excess / 100.0) + gpa_excess

    # --- Acceptance rate squared (non-linearity in selectivity) ---
    acceptance_rate_sq = acc ** 2

    # --- Yield × overqualification (yield protection signal) ---
    yr = yield_rate if yield_rate is not None else None
    yield_x_overqualification = yr * overqualification_index if yr is not None else None

    # --- Academic fit (school-relative combined z-score) ---
    academic_fit = (
        0.5 * np.clip(sat_zscore, -3, 3) / 3
        + 0.5 * np.clip(gpa_zscore_at_school, -3, 3) / 3
    )

    # --- Holistic signal (normalized SAT range width) ---
    holistic_signal = (sat_range / s_avg) if s_avg and s_avg > 0 and sat_range > 0 else None

    result = {
        "sat_percentile_at_school": np.clip(sat_percentile, -1, 2),
        "gpa_vs_expected": gpa_vs_expected,
        "selectivity_bucket": bucket,
        "sat_zscore_at_school": sat_zscore,
        "gpa_zscore_at_school": gpa_zscore_at_school,
        "gpa_x_acceptance": gpa_x_acceptance,
        "sat_percentile_sq": sat_percentile_sq,
        "selectivity_x_sat": selectivity_x_sat,
        "academic_composite_z": academic_composite_z,
        "competitiveness_index": competitiveness_index,
        "gpa_x_competitiveness": gpa_x_competitiveness,
        "sat_x_competitiveness": sat_x_competitiveness,
        "instate_x_public": instate_x_public,
        "residency_x_acceptance": residency_x_acceptance,
        # New features
        "sat_excess": sat_excess,
        "gpa_excess": gpa_excess,
        "sat_ratio": sat_ratio,
        "log_enrollment": log_enrollment,
        "log_earnings": log_earnings,
        "is_yield_protector": is_yield_protector,
        "niche_academics_ord": niche_academics_ord,
        "niche_value_ord": niche_value_ord,
        "niche_professors_ord": niche_professors_ord,
        "niche_diversity_ord": niche_diversity_ord,
        "niche_campus_ord": niche_campus_ord,
        "niche_overall_ord": niche_overall_ord,
        "niche_rank": niche_rank,
        "avg_annual_cost": avg_annual_cost,
        "cost_earnings_ratio": cost_earnings_ratio,
        # Binary threshold features
        "sat_above_75th": sat_above_75th,
        "sat_below_25th": sat_below_25th,
        "overqualification_index": overqualification_index,
        "acceptance_rate_sq": acceptance_rate_sq,
        "has_test_score": 1.0,  # always 1 in training; set at inference
        # Yield protection & fit signals
        "yield_x_overqualification": yield_x_overqualification,
        "academic_fit": academic_fit,
        "holistic_signal": holistic_signal,
        "sat_range": sat_range if sat_range > 0 else None,
    }
    return result


# ---------------------------------------------------------------------------
# DataFrame-level feature computation (used by data_pipeline.py for training)
# ---------------------------------------------------------------------------

def compute_features_df(
    df,          # type: pd.DataFrame
    z_stats=None,  # type: Optional[dict]
):
    # type: (...) -> tuple
    """Compute all engineered features on a DataFrame of applicant rows.

    Expects columns: sat_score, sat_25, sat_75, sat_avg, gpa,
    acceptance_rate, graduation_rate, school_avg_admitted_gpa (may have NaNs).

    Returns:
        (DataFrame with new feature columns added, z_stats dict)
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

    acc = df["acceptance_rate"].fillna(0.5)

    # SAT z-score at school
    iqr_std = sat_range / 1.35
    s_avg = df["sat_avg"].fillna((df["sat_25"] + df["sat_75"]) / 2)
    df["sat_zscore_at_school"] = (
        (df["sat_score"] - s_avg) / iqr_std.replace(0, float("nan"))
    ).fillna(0.0)

    # GPA z-score at school (NEW)
    avg_gpa = df["school_avg_admitted_gpa"].fillna(fallback)
    df["gpa_zscore_at_school"] = ((df["gpa"] - avg_gpa) / 0.5).fillna(0.0)

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

    # Competitiveness interactions
    df["gpa_x_competitiveness"] = df["gpa"] * df["competitiveness_index"]
    df["sat_x_competitiveness"] = df["sat_zscore_at_school"] * df["competitiveness_index"]

    # Residency interactions
    if "residency" in df.columns and "ownership" in df.columns:
        is_instate = (df["residency"] == "inState").astype(float)
        is_instate = is_instate.where(df["residency"].notna(), other=float("nan"))
        is_public = (df["ownership"] == 1).astype(float)
        df["instate_x_public"] = is_instate * is_public
        df["residency_x_acceptance"] = is_instate * acc
    else:
        df["instate_x_public"] = float("nan")
        df["residency_x_acceptance"] = float("nan")

    # --- Overqualification features (NEW) ---
    df["sat_excess"] = (df["sat_score"] - df["sat_75"]).clip(lower=0).fillna(0.0)
    df["gpa_excess"] = (df["gpa"] - df["school_avg_admitted_gpa"].fillna(4.0)).clip(lower=0)

    # --- SAT ratio (NEW) ---
    df["sat_ratio"] = (df["sat_score"] / s_avg.replace(0, float("nan"))).fillna(1.0)

    # --- Log transforms (NEW) ---
    if "enrollment" in df.columns:
        df["log_enrollment"] = np.log1p(df["enrollment"].fillna(0))
    if "median_earnings_10yr" in df.columns:
        df["log_earnings"] = np.log1p(df["median_earnings_10yr"].fillna(0))

    # --- Yield protector flag (NEW) ---
    if "school_name" in df.columns:
        df["is_yield_protector"] = df["school_name"].str.lower().str.strip().isin(
            YIELD_PROTECTOR_NAMES
        ).astype(float)
    else:
        df["is_yield_protector"] = 0.0

    # --- Major tier (NEW) ---
    if "major" in df.columns:
        df["major_tier"] = df["major"].apply(major_to_tier)

    # --- Niche grade ordinal features (NEW) ---
    niche_grade_cols = [
        "niche_academics_ord", "niche_value_ord", "niche_professors_ord",
        "niche_diversity_ord", "niche_campus_ord", "niche_overall_ord",
    ]
    source_cols = ["academics", "value", "professors", "diversity", "campus", "overall_grade"]
    for target_col, source_col in zip(niche_grade_cols, source_cols):
        if source_col in df.columns:
            df[target_col] = df[source_col].apply(grade_to_ordinal)
        else:
            df[target_col] = None

    # --- Cost affordability ratio (NEW) ---
    if "avg_annual_cost" in df.columns and "median_earnings_10yr" in df.columns:
        earnings = df["median_earnings_10yr"].replace(0, float("nan"))
        df["cost_earnings_ratio"] = (
            df["avg_annual_cost"].fillna(0) / earnings
        ).fillna(0.0)
    else:
        df["cost_earnings_ratio"] = None

    # --- Major tier × acceptance rate interaction (NEW) ---
    if "major_tier" in df.columns:
        is_stem_comp = (df["major_tier"] == "stem_competitive").astype(float)
        is_stem_comp = is_stem_comp.where(df["major_tier"].notna(), other=float("nan"))
        df["stem_competitive_x_acceptance"] = is_stem_comp * acc

    # --- Binary threshold features ---
    df["sat_above_75th"] = (df["sat_score"] > df["sat_75"]).astype(float)
    df["sat_below_25th"] = (df["sat_score"] < df["sat_25"]).astype(float)

    # --- Overqualification composite ---
    df["overqualification_index"] = (df["sat_excess"] / 100.0) + df["gpa_excess"]

    # --- Acceptance rate squared (non-linearity in selectivity) ---
    df["acceptance_rate_sq"] = acc ** 2

    # --- Test score presence flag (always 1 in training data) ---
    df["has_test_score"] = 1.0

    # --- Yield × overqualification (yield protection signal) ---
    if "yield_rate" in df.columns:
        df["yield_x_overqualification"] = (
            df["yield_rate"] * df["overqualification_index"]
        )
    else:
        df["yield_x_overqualification"] = None

    # --- Academic fit (school-relative combined z-score) ---
    df["academic_fit"] = (
        0.5 * df["sat_zscore_at_school"].clip(-3, 3) / 3
        + 0.5 * df["gpa_zscore_at_school"].clip(-3, 3) / 3
    )

    # --- Holistic signal (normalized SAT range width) ---
    df["holistic_signal"] = (sat_range / s_avg.replace(0, float("nan"))).where(
        sat_range > 0
    )

    # --- SAT range as explicit feature ---
    df["sat_range"] = sat_range.where(sat_range > 0)

    return df, z_stats
