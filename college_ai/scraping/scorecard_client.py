"""
College Scorecard API client.

Pulls school-level admissions, demographics, and outcomes data for all
Title IV institutions (~6,500 schools).

Usage:
    python -m college_ai.scraping.scorecard_client

Requires SCORECARD_API_KEY in .env (register free at https://api.data.gov/signup/).
"""

import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
from sqlalchemy import select

from college_ai.db.connection import get_session, init_db, with_retry
from college_ai.db.models import School

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"

# Fields to pull — mapped to our School model columns (category-prefixed)
FIELDS = ",".join([
    # core identity
    "id",
    "school.name",
    "school.city",
    "school.state",
    "school.ownership",
    # identity_
    "school.alias",
    "school.school_url",
    "school.locale",
    "school.carnegie_basic",
    "school.religious_affiliation",
    # admissions_
    "latest.admissions.admission_rate.overall",
    "latest.admissions.sat_scores.average.overall",
    "latest.admissions.sat_scores.25th_percentile.critical_reading",
    "latest.admissions.sat_scores.75th_percentile.critical_reading",
    "latest.admissions.sat_scores.25th_percentile.math",
    "latest.admissions.sat_scores.75th_percentile.math",
    "latest.admissions.act_scores.25th_percentile.cumulative",
    "latest.admissions.act_scores.75th_percentile.cumulative",
    "latest.admissions.test_requirements",
    # student_
    "latest.student.size",
    "latest.student.retention_rate.four_year.full_time",
    "latest.student.demographics.student_faculty_ratio",
    "latest.student.demographics.age_entry",
    "latest.student.demographics.men",
    "latest.student.demographics.women",
    "latest.student.part_time_share",
    "latest.student.demographics.race_ethnicity.white",
    "latest.student.demographics.race_ethnicity.black",
    "latest.student.demographics.race_ethnicity.hispanic",
    "latest.student.demographics.race_ethnicity.asian",
    "latest.student.demographics.first_generation",
    # cost_
    "latest.cost.tuition.in_state",
    "latest.cost.tuition.out_of_state",
    "latest.cost.attendance.academic_year",
    "latest.cost.avg_net_price.overall",
    "latest.cost.booksupply",
    "latest.cost.net_price.consumer.by_income_level.0-30000",
    "latest.cost.net_price.consumer.by_income_level.30001-48000",
    "latest.cost.net_price.consumer.by_income_level.48001-75000",
    "latest.cost.net_price.consumer.by_income_level.750001-111000",
    "latest.cost.net_price.consumer.by_income_level.110001-plus",
    # aid_
    "latest.aid.pell_grant_rate",
    "latest.aid.federal_loan_rate",
    "latest.aid.median_debt.completers.overall",
    "latest.aid.cumulative_debt.25th_percentile",
    "latest.aid.cumulative_debt.75th_percentile",
    # outcome_
    "latest.completion.consumer_rate",
    "latest.earnings.10_yrs_after_entry.median",
    # institution_
    "school.endowment.end",
    "school.faculty_salary",
    "school.ft_faculty_rate",
    "school.instructional_expenditure_per_fte",
])

PER_PAGE = 100
REQUEST_DELAY = 0.5  # seconds between pages
SCORECARD_WORKERS = int(os.getenv("SCORECARD_WORKERS", "3"))
MAX_RETRIES = 3


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


import re as _re

_CAMPUS_SUFFIX_RE = _re.compile(
    r"\s*[-\u2013\u2014]\s*"
    r"(main\s+campus|central\s+campus|flagship|"
    r"all\s+campuses|global\s+campus|online)\s*$",
    _re.IGNORECASE,
)


def _clean_school_name(name: Optional[str]) -> Optional[str]:
    """Strip campus suffixes like '-Main Campus' from Scorecard school names."""
    if not name:
        return name
    return _CAMPUS_SUFFIX_RE.sub("", name).strip()


def _parse_school(result: dict) -> dict:
    """Convert a flat API result dict into School model kwargs."""
    sat_avg, sat_25, sat_75 = _compute_sat_composite(result)

    return dict(
        id=result["id"],
        name=_clean_school_name(_get(result, "school.name")),
        city=_get(result, "school.city"),
        state=_get(result, "school.state"),
        ownership=_get(result, "school.ownership"),
        # identity_
        identity_alias=_get(result, "school.alias"),
        identity_url=_get(result, "school.school_url"),
        identity_locale=_get(result, "school.locale"),
        identity_carnegie_basic=_get(result, "school.carnegie_basic"),
        identity_religious_affiliation=_get(result, "school.religious_affiliation"),
        # admissions_
        admissions_rate=_get(result, "latest.admissions.admission_rate.overall"),
        admissions_sat_avg=sat_avg,
        admissions_sat_25=sat_25,
        admissions_sat_75=sat_75,
        admissions_act_25=_get(result, "latest.admissions.act_scores.25th_percentile.cumulative"),
        admissions_act_75=_get(result, "latest.admissions.act_scores.75th_percentile.cumulative"),
        admissions_test_requirements=_get(result, "latest.admissions.test_requirements"),
        # student_
        student_size=_get(result, "latest.student.size"),
        student_retention_rate=_get(result, "latest.student.retention_rate.four_year.full_time"),
        student_faculty_ratio=_get(result, "latest.student.demographics.student_faculty_ratio"),
        student_avg_age_entry=_get(result, "latest.student.demographics.age_entry"),
        student_pct_men=_get(result, "latest.student.demographics.men"),
        student_pct_women=_get(result, "latest.student.demographics.women"),
        student_part_time_share=_get(result, "latest.student.part_time_share"),
        student_pct_white=_get(result, "latest.student.demographics.race_ethnicity.white"),
        student_pct_black=_get(result, "latest.student.demographics.race_ethnicity.black"),
        student_pct_hispanic=_get(result, "latest.student.demographics.race_ethnicity.hispanic"),
        student_pct_asian=_get(result, "latest.student.demographics.race_ethnicity.asian"),
        student_pct_first_gen=_get(result, "latest.student.demographics.first_generation"),
        # cost_
        cost_tuition_in_state=_get(result, "latest.cost.tuition.in_state"),
        cost_tuition_out_of_state=_get(result, "latest.cost.tuition.out_of_state"),
        cost_attendance=_get(result, "latest.cost.attendance.academic_year"),
        cost_avg_net_price=_get(result, "latest.cost.avg_net_price.overall"),
        cost_booksupply=_get(result, "latest.cost.booksupply"),
        cost_net_price_0_30k=_get(result, "latest.cost.net_price.consumer.by_income_level.0-30000"),
        cost_net_price_30_48k=_get(result, "latest.cost.net_price.consumer.by_income_level.30001-48000"),
        cost_net_price_48_75k=_get(result, "latest.cost.net_price.consumer.by_income_level.48001-75000"),
        cost_net_price_75_110k=_get(result, "latest.cost.net_price.consumer.by_income_level.750001-111000"),
        cost_net_price_110k_plus=_get(result, "latest.cost.net_price.consumer.by_income_level.110001-plus"),
        # aid_
        aid_pell_grant_rate=_get(result, "latest.aid.pell_grant_rate"),
        aid_federal_loan_rate=_get(result, "latest.aid.federal_loan_rate"),
        aid_median_debt=_get(result, "latest.aid.median_debt.completers.overall"),
        aid_cumulative_debt_25th=_get(result, "latest.aid.cumulative_debt.25th_percentile"),
        aid_cumulative_debt_75th=_get(result, "latest.aid.cumulative_debt.75th_percentile"),
        # outcome_
        outcome_graduation_rate=_get(result, "latest.completion.consumer_rate"),
        outcome_median_earnings_10yr=_get(result, "latest.earnings.10_yrs_after_entry.median"),
        # institution_
        institution_endowment=_get(result, "school.endowment.end"),
        institution_faculty_salary=_get(result, "school.faculty_salary"),
        institution_ft_faculty_rate=_get(result, "school.ft_faculty_rate"),
        institution_instructional_spend_per_fte=_get(result, "school.instructional_expenditure_per_fte"),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def _fetch_page(api_key: str, page: int) -> dict:
    """Fetch a single page from the Scorecard API with retry."""
    for attempt in range(MAX_RETRIES):
        try:
            params = {
                "api_key": api_key,
                "fields": FIELDS,
                "per_page": PER_PAGE,
                "page": page,
            }
            resp = requests.get(BASE_URL, params=params, timeout=30)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                logger.warning(f"Rate limited on page {page}, waiting {retry_after}s")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning(f"Page {page} failed: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    return {}


def _upsert_parsed(parsed: list[dict]):
    """Bulk upsert a page of parsed school data."""
    def _upsert_page(session, _parsed=parsed):
        for school_data in _parsed:
            existing = session.get(School, school_data["id"])
            if existing:
                for k, v in school_data.items():
                    setattr(existing, k, v)
            else:
                session.add(School(**school_data))
    with_retry(_upsert_page)


def fetch_all_schools() -> int:
    """Fetch all schools from College Scorecard API and upsert into DB.
    Uses concurrent page fetching for speed.

    Returns the total number of schools ingested.
    """
    api_key = _get_api_key()
    init_db()

    # Fetch first page to determine total
    logger.info("Fetching page 0 (to determine total pages)...")
    first_data = _fetch_page(api_key, 0)
    results = first_data.get("results", [])
    if not results:
        logger.info("No results from Scorecard API.")
        return 0

    metadata = first_data.get("metadata", {})
    total_records = metadata.get("total", 0)
    total_pages = (total_records // PER_PAGE) + 1

    # Parse and save first page
    parsed = [_parse_school(r) for r in results if _parse_school(r).get("name")]
    _upsert_parsed(parsed)
    total_ingested = len(parsed)
    logger.info(f"  Page 0: ingested {total_ingested}/{total_records} schools total")

    if total_pages <= 1:
        logger.info(f"Done. {total_ingested} schools ingested into DB.")
        return total_ingested

    # Fetch remaining pages in parallel
    remaining_pages = list(range(1, total_pages))
    num_workers = min(SCORECARD_WORKERS, len(remaining_pages))
    logger.info(f"Fetching {len(remaining_pages)} remaining pages with {num_workers} workers...")

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        future_to_page = {
            executor.submit(_fetch_page, api_key, p): p
            for p in remaining_pages
        }

        for fut in as_completed(future_to_page):
            page_num = future_to_page[fut]
            try:
                data = fut.result()
                page_results = data.get("results", [])
                if not page_results:
                    continue

                page_parsed = [
                    _parse_school(r) for r in page_results
                    if _parse_school(r).get("name")
                ]
                _upsert_parsed(page_parsed)
                total_ingested += len(page_parsed)
                logger.info(
                    f"  Page {page_num}: ingested {total_ingested}/{total_records} schools total"
                )
            except Exception as e:
                logger.error(f"  Page {page_num} FAILED: {e}")

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
