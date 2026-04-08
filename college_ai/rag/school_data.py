"""
Fetch and format school data from the Turso DB for RAG prompt injection.

Provides structured school statistics (admissions, financials, demographics)
as context for the LLM, so it can reference verified data without relying
solely on crawled web pages.

Two fetching modes:
  - Category-aware: selective fetch by column prefixes (non-ranking queries)
  - Batch: all base stats for multiple schools (ranking queries)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy singleton — avoids reloading ~6,500 schools on every call
_matcher = None  # type: Optional[Any]


def _get_matcher():
    global _matcher
    if _matcher is None:
        from college_ai.ml.school_matcher import SchoolMatcher
        _matcher = SchoolMatcher()
    return _matcher


_OWNERSHIP_MAP = {1: "Public", 2: "Private nonprofit", 3: "For-profit"}

_TEST_REQ_MAP = {
    1: "Required",
    2: "Recommended",
    3: "Neither required nor recommended",
    4: "Unknown",
    5: "Test-flexible (considered but not required)",
}

# Maps ranking category name → school_data dict key for the Niche grade
_CATEGORY_TO_GRADE_KEY = {
    "academics": "academics_grade",
    "value": "value_grade",
    "diversity": "diversity_grade",
    "campus": "campus_grade",
    "athletics": "athletics_grade",
    "party_scene": "party_scene_grade",
    "professors": "professors_grade",
    "location": "location_grade",
    "dorms": "dorms_grade",
    "food": "food_grade",
    "student_life": "student_life_grade",
    "safety": "safety_grade",
}

_CATEGORY_LABELS = {
    "academics": "Academics",
    "value": "Value",
    "diversity": "Diversity",
    "campus": "Campus",
    "athletics": "Athletics",
    "party_scene": "Party Scene",
    "professors": "Professors",
    "location": "Location",
    "dorms": "Dorms",
    "food": "Food",
    "student_life": "Student Life",
    "safety": "Safety",
}

# ---------------------------------------------------------------------------
# Category → DB column mappings for selective fetching
# ---------------------------------------------------------------------------

# Each category maps to a list of (db_attr, output_key) pairs.
# "identity" is always included when a school is present.

_CATEGORY_COLUMNS = {
    "identity": [
        ("identity_acceptance_rate", "acceptance_rate"),
        ("identity_alias", "aliases"),
        ("identity_url", "url"),
        ("identity_locale", "locale"),
        ("identity_carnegie_basic", "carnegie_basic"),
        ("identity_religious_affiliation", "religious_affiliation"),
    ],
    "admissions": [
        ("admissions_sat_avg", "sat_avg"),
        ("admissions_sat_25", "sat_25"),
        ("admissions_sat_75", "sat_75"),
        ("admissions_act_25", "act_25"),
        ("admissions_act_75", "act_75"),
        ("admissions_test_requirements", "test_requirements"),
    ],
    "student": [
        ("student_size", "enrollment"),
        ("student_retention_rate", "retention_rate"),
        ("student_faculty_ratio", "student_faculty_ratio"),
        ("student_avg_age_entry", "avg_age_entry"),
        ("student_pct_men", "pct_men"),
        ("student_pct_women", "pct_women"),
        ("student_part_time_share", "part_time_share"),
        ("student_pct_white", "pct_white"),
        ("student_pct_black", "pct_black"),
        ("student_pct_hispanic", "pct_hispanic"),
        ("student_pct_asian", "pct_asian"),
        ("student_pct_first_gen", "pct_first_gen"),
    ],
    "cost": [
        ("cost_tuition_in_state", "tuition_in_state"),
        ("cost_tuition_out_of_state", "tuition_out_of_state"),
        ("cost_attendance", "cost_attendance"),
        ("cost_avg_net_price", "avg_net_price"),
        ("cost_booksupply", "booksupply"),
        ("cost_net_price_0_30k", "net_price_0_30k"),
        ("cost_net_price_30_48k", "net_price_30_48k"),
        ("cost_net_price_48_75k", "net_price_48_75k"),
        ("cost_net_price_75_110k", "net_price_75_110k"),
        ("cost_net_price_110k_plus", "net_price_110k_plus"),
    ],
    "aid": [
        ("aid_pell_grant_rate", "pell_grant_rate"),
        ("aid_federal_loan_rate", "federal_loan_rate"),
        ("aid_median_debt", "median_debt"),
        ("aid_cumulative_debt_25th", "cumulative_debt_25th"),
        ("aid_cumulative_debt_75th", "cumulative_debt_75th"),
    ],
    "outcome": [
        ("outcome_graduation_rate", "graduation_rate"),
        ("outcome_median_earnings_10yr", "median_earnings_10yr"),
    ],
    "institution": [
        ("institution_endowment", "endowment"),
        ("institution_faculty_salary", "faculty_salary"),
        ("institution_ft_faculty_rate", "ft_faculty_rate"),
        ("institution_instructional_spend_per_fte", "instructional_spend_per_fte"),
    ],
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct(val: Optional[float]) -> Optional[str]:
    """Format a 0-1 float as a percentage string."""
    if val is None:
        return None
    return f"{val * 100:.1f}%"


def _money(val) -> Optional[str]:
    """Format a number as a dollar amount."""
    if val is None:
        return None
    return f"${int(val):,}"


def _ratio(val: Optional[float]) -> Optional[str]:
    """Format a student-faculty ratio."""
    if val is None:
        return None
    return f"{val:.0f}:1"


# ---------------------------------------------------------------------------
# Category-aware fetching (non-ranking queries)
# ---------------------------------------------------------------------------

def _fetch_school_by_categories(school_id: int, categories: List[str]) -> Optional[Dict[str, Any]]:
    """Fetch a school by UNITID, returning only fields for the requested categories."""
    from college_ai.db.connection import get_session
    from college_ai.db.models import School

    session = get_session()
    try:
        school = session.get(School, school_id)
        if school is None:
            return None

        # Base fields always included
        data = {
            "school_id": school.id,
            "name": school.name,
            "city": school.city,
            "state": school.state,
            "ownership": school.ownership,
        }

        # Fetch only columns for requested categories
        for cat in categories:
            columns = _CATEGORY_COLUMNS.get(cat, [])
            for db_attr, output_key in columns:
                data[output_key] = getattr(school, db_attr, None)

        return data
    finally:
        session.close()


def fetch_school_data_by_categories(
    school_name: str,
    categories: List[str],
) -> Optional[Dict[str, Any]]:
    """Fuzzy-match *school_name* and return fields for the requested categories.

    Always includes base fields (name, city, state, ownership) and identity
    category. Additional categories control which column groups are fetched.

    Returns ``None`` if no match is found in the DB.
    """
    try:
        matcher = _get_matcher()
        school_id = matcher.match(school_name)
        if school_id is None:
            return None
        # Ensure identity is always included
        cats = list(dict.fromkeys(["identity"] + categories))
        return _fetch_school_by_categories(school_id, cats)
    except Exception:
        logger.debug("Failed to fetch school data for %r", school_name, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Category-aware formatting (non-ranking queries)
# ---------------------------------------------------------------------------

def format_school_data_block_by_categories(
    data: Dict[str, Any],
    categories: List[str],
) -> str:
    """Format school data as a human-readable block, showing only relevant categories.

    Each category becomes a labeled section with its fields.
    """
    lines = [f"[SCHOOL DATA] {data['name']}"]

    # Location & type (always shown)
    loc_parts = []
    if data.get("city") and data.get("state"):
        loc_parts.append(f"Location: {data['city']}, {data['state']}")
    elif data.get("state"):
        loc_parts.append(f"State: {data['state']}")
    ownership = _OWNERSHIP_MAP.get(data.get("ownership"))
    if ownership:
        loc_parts.append(f"Type: {ownership}")
    if loc_parts:
        lines.append(" | ".join(loc_parts))

    # Identity
    if "identity" in categories:
        parts = []
        ar = _pct(data.get("acceptance_rate"))
        if ar:
            parts.append(f"Acceptance Rate: {ar}")
        url = data.get("url")
        if url:
            parts.append(f"Website: {url}")
        if parts:
            lines.append(" | ".join(parts))

    # Admissions / test scores
    if "admissions" in categories:
        parts = []
        sat_avg = data.get("sat_avg")
        if sat_avg:
            parts.append(f"SAT Avg: {int(sat_avg)}")
        sat_25, sat_75 = data.get("sat_25"), data.get("sat_75")
        if sat_25 and sat_75:
            parts.append(f"SAT Range: {int(sat_25)}-{int(sat_75)}")
        act_25, act_75 = data.get("act_25"), data.get("act_75")
        if act_25 and act_75:
            parts.append(f"ACT Range: {int(act_25)}-{int(act_75)}")
        tr = data.get("test_requirements")
        if tr:
            tr_label = _TEST_REQ_MAP.get(tr)
            if tr_label:
                parts.append(f"Test Policy: {tr_label}")
        if parts:
            lines.append("Test Scores: " + " | ".join(parts))

    # Student / enrollment & demographics
    if "student" in categories:
        enroll_parts = []
        enrollment = data.get("enrollment")
        if enrollment:
            enroll_parts.append(f"Enrollment: {enrollment:,}")
        sfr = _ratio(data.get("student_faculty_ratio"))
        if sfr:
            enroll_parts.append(f"Student-Faculty Ratio: {sfr}")
        rr = _pct(data.get("retention_rate"))
        if rr:
            enroll_parts.append(f"Retention Rate: {rr}")
        avg_age = data.get("avg_age_entry")
        if avg_age:
            enroll_parts.append(f"Avg Age at Entry: {avg_age}")
        pct_m = _pct(data.get("pct_men"))
        pct_w = _pct(data.get("pct_women"))
        if pct_m and pct_w:
            enroll_parts.append(f"Male/Female: {pct_m}/{pct_w}")
        pt = _pct(data.get("part_time_share"))
        if pt:
            enroll_parts.append(f"Part-Time: {pt}")
        if enroll_parts:
            lines.append("Enrollment: " + " | ".join(enroll_parts))

        # Demographics sub-section
        demo_parts = []
        for key, label in [
            ("pct_white", "White"), ("pct_black", "Black"),
            ("pct_hispanic", "Hispanic"), ("pct_asian", "Asian"),
        ]:
            p = _pct(data.get(key))
            if p:
                demo_parts.append(f"{label} {p}")
        fg = _pct(data.get("pct_first_gen"))
        if fg:
            demo_parts.append(f"First-Gen {fg}")
        if demo_parts:
            lines.append("Demographics: " + ", ".join(demo_parts))

    # Cost / tuition & net price
    if "cost" in categories:
        parts = []
        tis = _money(data.get("tuition_in_state"))
        tos = _money(data.get("tuition_out_of_state"))
        if tis and tos and tis != tos:
            parts.append(f"In-State Tuition: {tis}")
            parts.append(f"Out-of-State Tuition: {tos}")
        elif tis:
            parts.append(f"Tuition: {tis}")
        elif tos:
            parts.append(f"Tuition: {tos}")
        coa = _money(data.get("cost_attendance"))
        if coa:
            parts.append(f"Total Cost of Attendance: {coa}")
        anp = _money(data.get("avg_net_price"))
        if anp:
            parts.append(f"Avg Net Price: {anp}")
        books = _money(data.get("booksupply"))
        if books:
            parts.append(f"Books & Supplies: {books}")
        if parts:
            lines.append("Cost: " + " | ".join(parts))

        # Net price by income bracket
        bracket_parts = []
        for key, label in [
            ("net_price_0_30k", "$0-30k"),
            ("net_price_30_48k", "$30-48k"),
            ("net_price_48_75k", "$48-75k"),
            ("net_price_75_110k", "$75-110k"),
            ("net_price_110k_plus", "$110k+"),
        ]:
            val = _money(data.get(key))
            if val:
                bracket_parts.append(f"{label}: {val}")
        if bracket_parts:
            lines.append("Net Price by Income: " + " | ".join(bracket_parts))

    # Aid / financial aid & debt
    if "aid" in categories:
        parts = []
        pell = _pct(data.get("pell_grant_rate"))
        if pell:
            parts.append(f"Pell Grant Rate: {pell}")
        loan = _pct(data.get("federal_loan_rate"))
        if loan:
            parts.append(f"Federal Loan Rate: {loan}")
        debt = _money(data.get("median_debt"))
        if debt:
            parts.append(f"Median Debt at Graduation: {debt}")
        d25 = _money(data.get("cumulative_debt_25th"))
        d75 = _money(data.get("cumulative_debt_75th"))
        if d25 and d75:
            parts.append(f"Debt Range (25th-75th): {d25}-{d75}")
        if parts:
            lines.append("Financial Aid: " + " | ".join(parts))

    # Outcome / graduation & earnings
    if "outcome" in categories:
        parts = []
        gr = _pct(data.get("graduation_rate"))
        if gr:
            parts.append(f"Graduation Rate: {gr}")
        earn = _money(data.get("median_earnings_10yr"))
        if earn:
            parts.append(f"Median Earnings (10yr post-entry): {earn}")
        if parts:
            lines.append("Outcomes: " + " | ".join(parts))

    # Institution / resources & faculty
    if "institution" in categories:
        parts = []
        endow = data.get("endowment")
        if endow:
            if endow >= 1_000_000_000:
                parts.append(f"Endowment: ${endow / 1_000_000_000:.1f}B")
            elif endow >= 1_000_000:
                parts.append(f"Endowment: ${endow / 1_000_000:.0f}M")
            else:
                parts.append(f"Endowment: {_money(endow)}")
        salary = _money(data.get("faculty_salary"))
        if salary:
            parts.append(f"Avg Faculty Salary: {salary}/mo")
        ft = _pct(data.get("ft_faculty_rate"))
        if ft:
            parts.append(f"Full-Time Faculty: {ft}")
        spend = _money(data.get("instructional_spend_per_fte"))
        if spend:
            parts.append(f"Instructional Spend/Student: {spend}")
        if parts:
            lines.append("Resources: " + " | ".join(parts))

    return "\n".join(lines) + "\n\n"


def format_multi_school_data_block_by_categories(
    school_data_map: Dict[str, Dict[str, Any]],
    school_names: List[str],
    categories: List[str],
) -> str:
    """Format school data blocks for multiple schools.

    Calls ``format_school_data_block_by_categories`` per school and
    concatenates the results. Each school gets its own ``[SCHOOL DATA]``
    header so the LLM can distinguish them.
    """
    blocks = []
    for name in school_names:
        sd = school_data_map.get(name.lower())
        if sd:
            blocks.append(format_school_data_block_by_categories(sd, categories))
    return "\n".join(blocks)


def format_niche_grades_block(
    school_data_map: Dict[str, Dict[str, Any]],
    hits: List[Dict[str, Any]],
    niche_categories: List[str],
) -> str:
    """Format Niche grades for ranking queries as a separate LLM block.

    The block is clearly labeled so the LLM knows these grades are for
    ranking purposes only and must never be mentioned in the response.
    """
    if not niche_categories or niche_categories == ["other"]:
        return ""

    seen_ids = set()  # type: set
    lines = [
        "[NICHE GRADES — for internal ranking only, NEVER mention in response]",
    ]

    for hit in hits:
        college = (hit.get("college_name") or "").lower()
        sd = school_data_map.get(college)
        if sd is None:
            continue

        sid = sd.get("school_id")
        if sid in seen_ids:
            continue
        seen_ids.add(sid)

        grade_parts = []
        for cat in niche_categories:
            if cat == "other":
                continue
            grade_key = _CATEGORY_TO_GRADE_KEY.get(cat)
            label = _CATEGORY_LABELS.get(cat)
            if grade_key and label:
                grade_val = sd.get(grade_key)
                if grade_val:
                    grade_parts.append(f"{label} {grade_val}")
        if grade_parts:
            niche_rank = sd.get("niche_rank")
            rank_str = f" (#{niche_rank})" if niche_rank else ""
            lines.append(f"{sd['name']}{rank_str}: {', '.join(grade_parts)}")

    if len(lines) <= 1:
        return ""
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Batch fetching (used by reranker for Niche grade boosting)
# ---------------------------------------------------------------------------

def _fetch_school_row(school_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single School + NicheGrade row by UNITID. Returns flat dict."""
    from college_ai.db.connection import get_session
    from college_ai.db.models import School

    session = get_session()
    try:
        school = session.get(School, school_id)
        if school is None:
            return None

        data = {
            "school_id": school.id,
            "name": school.name,
            "city": school.city,
            "state": school.state,
            "ownership": school.ownership,
            # admissions
            "acceptance_rate": school.identity_acceptance_rate,
            "sat_avg": school.admissions_sat_avg,
            "sat_25": school.admissions_sat_25,
            "sat_75": school.admissions_sat_75,
            "act_25": school.admissions_act_25,
            "act_75": school.admissions_act_75,
            "test_requirements": school.admissions_test_requirements,
            # student
            "enrollment": school.student_size,
            "retention_rate": school.student_retention_rate,
            "student_faculty_ratio": school.student_faculty_ratio,
            # cost
            "tuition_in_state": school.cost_tuition_in_state,
            "tuition_out_of_state": school.cost_tuition_out_of_state,
            "cost_attendance": school.cost_attendance,
            "avg_net_price": school.cost_avg_net_price,
            # outcome
            "graduation_rate": school.outcome_graduation_rate,
            "median_earnings_10yr": school.outcome_median_earnings_10yr,
            # demographics
            "pct_white": school.student_pct_white,
            "pct_black": school.student_pct_black,
            "pct_hispanic": school.student_pct_hispanic,
            "pct_asian": school.student_pct_asian,
            "pct_first_gen": school.student_pct_first_gen,
            # aid
            "pell_grant_rate": school.aid_pell_grant_rate,
            "median_debt": school.aid_median_debt,
            # institution
            "endowment": school.institution_endowment,
        }

        ng = school.niche_grade
        if ng and not ng.no_data:
            data.update({
                "overall_grade": ng.overall_grade,
                "niche_rank": ng.niche_rank,
                "academics_grade": ng.academics,
                "value_grade": ng.value,
                "diversity_grade": ng.diversity,
                "campus_grade": ng.campus,
                "athletics_grade": ng.athletics,
                "party_scene_grade": ng.party_scene,
                "professors_grade": ng.professors,
                "location_grade": ng.location,
                "dorms_grade": ng.dorms,
                "food_grade": ng.food,
                "student_life_grade": ng.student_life,
                "safety_grade": ng.safety,
                "acceptance_rate_niche": ng.acceptance_rate_niche,
                "avg_annual_cost": ng.avg_annual_cost,
                "graduation_rate_niche": ng.graduation_rate_niche,
                "student_faculty_ratio_niche": ng.student_faculty_ratio_niche,
                "setting": ng.setting,
                "religious_affiliation": ng.religious_affiliation,
                "pct_students_on_campus": ng.pct_students_on_campus,
                "pct_greek_life": ng.pct_greek_life,
                "avg_rating": ng.avg_rating,
                "review_count": ng.review_count,
            })

        return data
    finally:
        session.close()


def fetch_school_data(school_name: str) -> Optional[Dict[str, Any]]:
    """Fuzzy-match *school_name* and return a flat dict of school + Niche data.

    Returns ``None`` if no match is found in the DB.
    """
    try:
        matcher = _get_matcher()
        school_id = matcher.match(school_name)
        if school_id is None:
            return None
        return _fetch_school_row(school_id)
    except Exception:
        logger.debug("Failed to fetch school data for %r", school_name, exc_info=True)
        return None


def fetch_school_data_batch(school_names: List[str]) -> Dict[str, Dict[str, Any]]:
    """Batch-fetch multiple schools. Returns ``{lowercased_name: data_dict}``.

    Deduplicates by UNITID so the same school isn't queried twice even if
    different hit chunks spell the name differently.
    """
    result = {}  # type: Dict[str, Dict[str, Any]]
    if not school_names:
        return result

    try:
        matcher = _get_matcher()

        id_to_names = {}  # type: Dict[int, List[str]]
        for name in school_names:
            sid = matcher.match(name)
            if sid is not None:
                id_to_names.setdefault(sid, []).append(name.lower())

        for sid, names in id_to_names.items():
            try:
                data = _fetch_school_row(sid)
                if data:
                    for n in names:
                        result[n] = data
                    result[data["name"].lower()] = data
            except Exception:
                logger.debug("Failed to fetch school %d", sid, exc_info=True)

    except Exception:
        logger.debug("Batch school data fetch failed", exc_info=True)

    return result


