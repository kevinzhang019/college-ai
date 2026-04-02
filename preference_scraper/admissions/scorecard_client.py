"""
College Scorecard API client.

Pulls school-level admissions, demographics, and outcomes data for all
Title IV institutions (~6,500 schools).

Usage:
    python -m preference_scraper.admissions.scorecard_client

Requires SCORECARD_API_KEY in .env (register free at https://api.data.gov/signup/).
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
from sqlalchemy import select

from preference_scraper.admissions.db import get_session, init_db
from preference_scraper.admissions.models import School

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"

# Fields to pull — mapped to our School model columns
FIELDS = ",".join([
    "id",
    "school.name",
    "school.city",
    "school.state",
    "school.ownership",
    "latest.admissions.admission_rate.overall",
    "latest.admissions.sat_scores.average.overall",
    "latest.admissions.sat_scores.25th_percentile.critical_reading",
    "latest.admissions.sat_scores.75th_percentile.critical_reading",
    "latest.admissions.sat_scores.25th_percentile.math",
    "latest.admissions.sat_scores.75th_percentile.math",
    "latest.admissions.act_scores.25th_percentile.cumulative",
    "latest.admissions.act_scores.75th_percentile.cumulative",
    "latest.student.size",
    "latest.student.retention_rate.four_year.full_time",
    "latest.completion.consumer_rate",
    "latest.earnings.10_yrs_after_entry.median",
    "latest.cost.tuition.in_state",
    "latest.cost.tuition.out_of_state",
    "latest.student.demographics.student_faculty_ratio",
    "latest.student.demographics.race_ethnicity.white",
    "latest.student.demographics.race_ethnicity.black",
    "latest.student.demographics.race_ethnicity.hispanic",
    "latest.student.demographics.race_ethnicity.asian",
    "latest.student.demographics.first_generation",
    "latest.admissions.yield",
])

PER_PAGE = 100
REQUEST_DELAY = 0.5  # seconds between pages


def _get_api_key() -> str:
    key = os.getenv("SCORECARD_API_KEY", "")
    if not key:
        raise RuntimeError(
            "SCORECARD_API_KEY not set. Register free at https://api.data.gov/signup/ "
            "and add SCORECARD_API_KEY=<key> to your .env file."
        )
    return key


def _get(result: dict, key: str):
    """Safely get a dot-notation key from a flat API result dict."""
    return result.get(key)


def _compute_sat_composite(result: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Compute SAT composite 25th/75th from section scores (reading + math)."""
    avg = _get(result, "latest.admissions.sat_scores.average.overall")

    p25_cr = _get(result, "latest.admissions.sat_scores.25th_percentile.critical_reading")
    p25_m  = _get(result, "latest.admissions.sat_scores.25th_percentile.math")
    p75_cr = _get(result, "latest.admissions.sat_scores.75th_percentile.critical_reading")
    p75_m  = _get(result, "latest.admissions.sat_scores.75th_percentile.math")

    sat_25 = (p25_cr + p25_m) if (p25_cr and p25_m) else None
    sat_75 = (p75_cr + p75_m) if (p75_cr and p75_m) else None

    return avg, sat_25, sat_75


def _parse_school(result: dict) -> dict:
    """Convert a flat API result dict into School model kwargs."""
    sat_avg, sat_25, sat_75 = _compute_sat_composite(result)

    return dict(
        id=result["id"],
        name=_get(result, "school.name"),
        city=_get(result, "school.city"),
        state=_get(result, "school.state"),
        ownership=_get(result, "school.ownership"),
        acceptance_rate=_get(result, "latest.admissions.admission_rate.overall"),
        sat_avg=sat_avg,
        sat_25=sat_25,
        sat_75=sat_75,
        act_25=_get(result, "latest.admissions.act_scores.25th_percentile.cumulative"),
        act_75=_get(result, "latest.admissions.act_scores.75th_percentile.cumulative"),
        enrollment=_get(result, "latest.student.size"),
        retention_rate=_get(result, "latest.student.retention_rate.four_year.full_time"),
        graduation_rate=_get(result, "latest.completion.consumer_rate"),
        median_earnings_10yr=_get(result, "latest.earnings.10_yrs_after_entry.median"),
        tuition_in_state=_get(result, "latest.cost.tuition.in_state"),
        tuition_out_of_state=_get(result, "latest.cost.tuition.out_of_state"),
        student_faculty_ratio=_get(result, "latest.student.demographics.student_faculty_ratio"),
        pct_white=_get(result, "latest.student.demographics.race_ethnicity.white"),
        pct_black=_get(result, "latest.student.demographics.race_ethnicity.black"),
        pct_hispanic=_get(result, "latest.student.demographics.race_ethnicity.hispanic"),
        pct_asian=_get(result, "latest.student.demographics.race_ethnicity.asian"),
        pct_first_gen=_get(result, "latest.student.demographics.first_generation"),
        yield_rate=_get(result, "latest.admissions.yield"),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def fetch_all_schools() -> int:
    """Fetch all schools from College Scorecard API and upsert into DB.

    Returns the total number of schools ingested.
    """
    api_key = _get_api_key()
    init_db()
    session = get_session()

    page = 0
    total_ingested = 0

    try:
        while True:
            params = {
                "api_key": api_key,
                "fields": FIELDS,
                "per_page": PER_PAGE,
                "page": page,
            }

            logger.info(f"Fetching page {page} ...")
            resp = requests.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            if not results:
                break

            metadata = data.get("metadata", {})
            total = metadata.get("total", "?")

            for result in results:
                school_data = _parse_school(result)
                if not school_data.get("name"):
                    logger.debug(f"Skipping school id={school_data['id']} with null name")
                    continue
                existing = session.get(School, school_data["id"])
                if existing:
                    for k, v in school_data.items():
                        setattr(existing, k, v)
                else:
                    session.add(School(**school_data))
                total_ingested += 1

            session.commit()
            logger.info(
                f"  Page {page}: ingested {total_ingested}/{total} schools total"
            )

            # Check if we've fetched all pages
            total_pages = metadata.get("total", 0) // PER_PAGE + 1
            page += 1
            if page >= total_pages:
                break

            time.sleep(REQUEST_DELAY)

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    logger.info(f"Done. {total_ingested} schools ingested into DB.")
    return total_ingested


def get_school_count() -> int:
    """Return count of schools in DB."""
    session = get_session()
    try:
        return session.query(School).count()
    finally:
        session.close()


if __name__ == "__main__":
    fetch_all_schools()
