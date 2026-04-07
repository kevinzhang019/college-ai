"""
Fetch and format school data from the Turso DB for RAG prompt injection.

Provides structured school statistics (admissions, financials, demographics)
as context for the LLM, so it can reference verified data without relying
solely on crawled web pages.

For ranking queries, also provides Niche grades for the specific categories
detected (e.g. academics, athletics) across all schools in the results.
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
            "acceptance_rate": school.acceptance_rate,
            "sat_avg": school.sat_avg,
            "sat_25": school.sat_25,
            "sat_75": school.sat_75,
            "act_25": school.act_25,
            "act_75": school.act_75,
            "enrollment": school.enrollment,
            "retention_rate": school.retention_rate,
            "graduation_rate": school.graduation_rate,
            "student_faculty_ratio": school.student_faculty_ratio,
            "tuition_in_state": school.tuition_in_state,
            "tuition_out_of_state": school.tuition_out_of_state,
            "median_earnings_10yr": school.median_earnings_10yr,
            "pct_white": school.pct_white,
            "pct_black": school.pct_black,
            "pct_hispanic": school.pct_hispanic,
            "pct_asian": school.pct_asian,
            "pct_first_gen": school.pct_first_gen,
            "yield_rate": school.yield_rate,
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

        # Deduplicate: map each name → school_id, then fetch unique IDs
        id_to_names = {}  # type: Dict[int, List[str]]
        for name in school_names:
            sid = matcher.match(name)
            if sid is not None:
                id_to_names.setdefault(sid, []).append(name.lower())

        for sid, names in id_to_names.items():
            try:
                data = _fetch_school_row(sid)
                if data:
                    # Map both the matched names and the canonical name
                    for n in names:
                        result[n] = data
                    result[data["name"].lower()] = data
            except Exception:
                logger.debug("Failed to fetch school %d", sid, exc_info=True)

    except Exception:
        logger.debug("Batch school data fetch failed", exc_info=True)

    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct(val: Optional[float]) -> Optional[str]:
    """Format a 0-1 float as a percentage string."""
    if val is None:
        return None
    return f"{val * 100:.1f}%"


def _money(val: Optional[int]) -> Optional[str]:
    """Format an integer as a dollar amount."""
    if val is None:
        return None
    return f"${val:,}"


def _ratio(val: Optional[float]) -> Optional[str]:
    """Format a student-faculty ratio."""
    if val is None:
        return None
    return f"{val:.0f}:1"


def _format_base_stats(data: Dict[str, Any]) -> List[str]:
    """Format the schools-table portion of a school data dict as lines.

    Returns a list of formatted lines (no header). Skips None fields.
    """
    lines = []

    # Location & type
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

    # Admissions
    adm_parts = []
    ar = _pct(data.get("acceptance_rate"))
    if ar:
        adm_parts.append(f"Acceptance Rate: {ar}")
    sat_avg = data.get("sat_avg")
    if sat_avg:
        adm_parts.append(f"SAT Avg: {int(sat_avg)}")
    sat_25, sat_75 = data.get("sat_25"), data.get("sat_75")
    if sat_25 and sat_75:
        adm_parts.append(f"SAT Range: {int(sat_25)}-{int(sat_75)}")
    act_25, act_75 = data.get("act_25"), data.get("act_75")
    if act_25 and act_75:
        adm_parts.append(f"ACT Range: {int(act_25)}-{int(act_75)}")
    if adm_parts:
        lines.append(" | ".join(adm_parts))

    # Enrollment & outcomes
    eo_parts = []
    enrollment = data.get("enrollment")
    if enrollment:
        eo_parts.append(f"Enrollment: {enrollment:,}")
    sfr = _ratio(data.get("student_faculty_ratio"))
    if sfr:
        eo_parts.append(f"Student-Faculty Ratio: {sfr}")
    gr = _pct(data.get("graduation_rate"))
    if gr:
        eo_parts.append(f"Graduation Rate: {gr}")
    rr = _pct(data.get("retention_rate"))
    if rr:
        eo_parts.append(f"Retention Rate: {rr}")
    if eo_parts:
        lines.append(" | ".join(eo_parts))

    # Financials
    fin_parts = []
    tis = _money(data.get("tuition_in_state"))
    tos = _money(data.get("tuition_out_of_state"))
    if tis and tos and tis != tos:
        fin_parts.append(f"Tuition (In-State): {tis}")
        fin_parts.append(f"Tuition (Out-of-State): {tos}")
    elif tis:
        fin_parts.append(f"Tuition: {tis}")
    elif tos:
        fin_parts.append(f"Tuition: {tos}")
    aac = _money(data.get("avg_annual_cost"))
    if aac:
        fin_parts.append(f"Avg Net Cost: {aac}")
    earn = _money(data.get("median_earnings_10yr"))
    if earn:
        fin_parts.append(f"Median Earnings (10yr): {earn}")
    if fin_parts:
        lines.append(" | ".join(fin_parts))

    # Demographics
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

    return lines


def format_school_data_block(data: Dict[str, Any]) -> str:
    """Format school data as a human-readable block for the LLM prompt.

    **Base stats only** — no Niche grades. Used for non-ranking queries.
    """
    lines = [f"[SCHOOL DATA] {data['name']}"]
    lines.extend(_format_base_stats(data))
    return "\n".join(lines) + "\n\n"


def format_ranking_school_data_block(
    school_data_map: Dict[str, Dict[str, Any]],
    hits: List[Dict[str, Any]],
    categories: List[str],
) -> str:
    """Format school data for all schools in *hits* for a ranking response.

    Includes base stats plus only the Niche grades matching *categories*.
    If categories == ["other"], omits all Niche grades.
    Deduplicates by school_id so each school appears once.
    """
    only_other = categories == ["other"]
    seen_ids = set()  # type: set
    blocks = []

    for hit in hits:
        college = (hit.get("college_name") or "").lower()
        sd = school_data_map.get(college)
        if sd is None:
            continue

        sid = sd.get("school_id")
        if sid in seen_ids:
            continue
        seen_ids.add(sid)

        # Header with rank if available
        niche_rank = sd.get("niche_rank")
        rank_str = f" (Rank #{niche_rank})" if niche_rank and not only_other else ""
        lines = [f"[SCHOOL DATA] {sd['name']}{rank_str}"]

        # Base stats
        lines.extend(_format_base_stats(sd))

        # Category-specific Niche grades (skip for "other")
        if not only_other:
            grade_parts = []
            for cat in categories:
                if cat == "other":
                    continue
                grade_key = _CATEGORY_TO_GRADE_KEY.get(cat)
                label = _CATEGORY_LABELS.get(cat)
                if grade_key and label:
                    grade_val = sd.get(grade_key)
                    if grade_val:
                        grade_parts.append(f"{label}: {grade_val}")
            if grade_parts:
                lines.append(" | ".join(grade_parts))

        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n\n"
