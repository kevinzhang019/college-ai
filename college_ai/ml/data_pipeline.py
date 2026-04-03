"""
Data processing pipeline for admissions ML model.

Cleans, normalizes, and exports training data.

Usage:
    python -m college_ai.ml.data_pipeline stats
    python -m college_ai.ml.data_pipeline export
    python -m college_ai.ml.data_pipeline export --format csv
"""

import os
import logging
import argparse
from typing import Optional

import pandas as pd
from sqlalchemy import text

from college_ai.db.connection import get_session, init_db, ENGINE
from college_ai.db.models import School, ApplicantDatapoint
from college_ai.ml.concordance import act_to_sat, sat_to_act
from college_ai.ml.feature_utils import compute_features_df

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)


def load_raw_data() -> pd.DataFrame:
    """Load all applicant datapoints joined with school features and niche grades."""
    init_db()  # ensure migrations (e.g. yield_rate column) are applied
    query = """
    SELECT
        a.id,
        a.school_id,
        a.source,
        a.gpa,
        a.sat_score,
        a.act_score,
        a.outcome,
        a.residency,
        a.major,
        s.name AS school_name,
        s.acceptance_rate,
        s.sat_avg,
        s.sat_25,
        s.sat_75,
        s.act_25,
        s.act_75,
        s.enrollment,
        s.retention_rate,
        s.graduation_rate,
        s.student_faculty_ratio,
        s.ownership,
        s.tuition_in_state,
        s.tuition_out_of_state,
        s.median_earnings_10yr,
        s.pct_white,
        s.pct_black,
        s.pct_hispanic,
        s.pct_asian,
        s.pct_first_gen,
        s.yield_rate,
        ng.overall_grade,
        ng.academics,
        ng.value,
        ng.diversity,
        ng.campus,
        ng.professors,
        ng.niche_rank,
        ng.setting,
        ng.avg_annual_cost,
        ng.religious_affiliation
    FROM applicant_datapoints a
    JOIN schools s ON a.school_id = s.id
    LEFT JOIN niche_grades ng ON a.school_id = ng.school_id AND COALESCE(ng.no_data, 0) = 0
    """
    df = pd.read_sql(query, ENGINE)
    logger.info(f"Loaded {len(df)} raw datapoints from {df['school_id'].nunique()} schools")
    niche_coverage = df["overall_grade"].notna().mean()
    logger.info(f"Niche grade coverage: {niche_coverage:.1%} of rows")
    return df


def normalize_test_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize test scores: ensure every row has a SAT score.

    - If only ACT: convert to SAT equivalent
    - If only SAT: keep as-is
    - If both: keep SAT, store ACT separately
    """
    df = df.copy()

    # Ensure numeric dtype before conversions
    df["sat_score"] = pd.to_numeric(df["sat_score"], errors="coerce")
    df["act_score"] = pd.to_numeric(df["act_score"], errors="coerce")

    # Convert ACT to SAT where SAT is missing
    mask_no_sat = df["sat_score"].isna() & df["act_score"].notna()
    if mask_no_sat.any():
        df.loc[mask_no_sat, "sat_score"] = (
            df.loc[mask_no_sat, "act_score"].apply(act_to_sat).astype(float)
        )

    # Fill ACT where missing (for records that only have SAT)
    mask_no_act = df["act_score"].isna() & df["sat_score"].notna()
    if mask_no_act.any():
        df.loc[mask_no_act, "act_score"] = (
            df.loc[mask_no_act, "sat_score"].apply(sat_to_act).astype(float)
        )

    # Drop rows with no test score at all
    before = len(df)
    df = df.dropna(subset=["sat_score"])
    dropped = before - len(df)
    if dropped:
        logger.info(f"Dropped {dropped} rows with no test scores")

    return df


def normalize_gpa(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize GPA values.

    - Cap weighted GPAs at 4.0 (add is_weighted flag)
    - Drop rows with invalid GPA
    """
    df = df.copy()

    # Flag likely weighted GPAs
    df["gpa_possibly_weighted"] = df["gpa"] > 4.0

    # Cap at 4.0 for model input, but keep original
    df["gpa_original"] = df["gpa"]
    df["gpa"] = df["gpa"].clip(upper=4.0)

    # Drop invalid GPAs
    before = len(df)
    df = df[(df["gpa"] > 0) & (df["gpa"] <= 4.0)]
    dropped = before - len(df)
    if dropped:
        logger.info(f"Dropped {dropped} rows with invalid GPA")

    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Create engineered features for model training."""
    df = df.copy()

    # Compute average admitted GPA per school from Niche scatterplot data
    niche_accepted = df[(df["source"] == "niche") & (df["outcome"] == "accepted")]
    school_avg_gpa = niche_accepted.groupby("school_id")["gpa"].mean().rename("school_avg_admitted_gpa")
    df = df.join(school_avg_gpa, on="school_id")
    coverage = df["school_avg_admitted_gpa"].notna().mean()
    logger.info(f"school_avg_admitted_gpa coverage: {coverage:.1%} of rows")

    # Compute all engineered features via shared utility
    df, z_stats = compute_features_df(df)
    logger.info(f"Z-normalization stats: {z_stats}")

    # Binary target
    df["admitted"] = (df["outcome"] == "accepted").astype(int)

    return df



def process_pipeline() -> pd.DataFrame:
    """Run the full data processing pipeline."""
    df = load_raw_data()

    if df.empty:
        logger.warning("No data to process!")
        return df

    # Drop waitlisted — ambiguous outcome, not useful for binary classification
    before = len(df)
    df = df[df["outcome"].isin(["accepted", "rejected"])]
    dropped = before - len(df)
    if dropped:
        logger.info(f"Dropped {dropped} waitlisted rows")

    logger.info("Normalizing test scores...")
    df = normalize_test_scores(df)

    logger.info("Normalizing GPA...")
    df = normalize_gpa(df)

    logger.info("Engineering features...")
    df = engineer_features(df)

    # Drop rows missing critical school features
    critical = ["acceptance_rate", "sat_25", "sat_75"]
    before = len(df)
    df = df.dropna(subset=critical)
    dropped = before - len(df)
    if dropped:
        logger.info(f"Dropped {dropped} rows missing critical school features")

    logger.info(f"Final dataset: {len(df)} rows, {df['school_id'].nunique()} schools")
    logger.info(f"Outcome distribution:\n{df['outcome'].value_counts().to_string()}")
    logger.info(f"Admitted rate: {df['admitted'].mean():.1%}")

    return df


def export(fmt: str = "parquet"):
    """Run pipeline and export to file."""
    df = process_pipeline()

    if df.empty:
        logger.warning("No data to export.")
        return

    if fmt == "parquet":
        path = os.path.join(DATA_DIR, "training_data.parquet")
        df.to_parquet(path, index=False)
    elif fmt == "csv":
        path = os.path.join(DATA_DIR, "training_data.csv")
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"Unknown format: {fmt}")

    logger.info(f"Exported {len(df)} rows to {path}")


def stats():
    """Print summary statistics about the current data."""
    session = get_session()
    try:
        school_count = session.query(School).count()
        dp_count = session.query(ApplicantDatapoint).count()

        print(f"Schools in DB: {school_count}")
        print(f"Applicant datapoints: {dp_count}")

        if dp_count > 0:
            # Source breakdown
            results = session.execute(
                text("SELECT source, COUNT(*) FROM applicant_datapoints GROUP BY source")
            ).fetchall()
            print("\nBy source:")
            for source, count in results:
                print(f"  {source}: {count}")

            # Outcome breakdown
            results = session.execute(
                text("SELECT outcome, COUNT(*) FROM applicant_datapoints GROUP BY outcome")
            ).fetchall()
            print("\nBy outcome:")
            for outcome, count in results:
                print(f"  {outcome}: {count}")

            # Top schools by datapoint count
            results = session.execute(
                text("""
                    SELECT s.name, COUNT(*) as cnt
                    FROM applicant_datapoints a
                    JOIN schools s ON a.school_id = s.id
                    GROUP BY s.name
                    ORDER BY cnt DESC
                    LIMIT 10
                """)
            ).fetchall()
            print("\nTop 10 schools by datapoints:")
            for name, count in results:
                print(f"  {name}: {count}")

    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Admissions data pipeline")
    parser.add_argument(
        "command",
        choices=["stats", "export"],
        help="'stats' to show data summary, 'export' to run pipeline and export",
    )
    parser.add_argument(
        "--format", choices=["parquet", "csv"], default="parquet",
        help="Export format (default: parquet)",
    )
    args = parser.parse_args()

    if args.command == "stats":
        stats()
    elif args.command == "export":
        export(fmt=args.format)


if __name__ == "__main__":
    main()
