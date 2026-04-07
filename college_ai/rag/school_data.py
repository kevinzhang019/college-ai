"""
Fetch and format school data from the Turso DB for RAG prompt injection.

Provides structured school statistics (admissions, financials, Niche grades)
as context for the LLM, so it can reference verified data without relying
solely on crawled web pages.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

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


def fetch_school_data(school_name: str) -> Optional[Dict[str, Any]]:
    """Fuzzy-match *school_name* and return a flat dict of school + Niche data.

    Returns ``None`` if no match is found in the DB.
    """
    try:
        matcher = _get_matcher()
        school_id = matcher.match(school_name)
        if school_id is None:
            return None

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

    except Exception:
        logger.debug("Failed to fetch school data for %r", school_name, exc_info=True)
        return None


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


def format_school_data_block(data: Dict[str, Any]) -> str:
    """Format a school data dict as a human-readable block for the LLM prompt.

    Skips any fields that are None so the block stays clean.
    """
    lines = [f"[SCHOOL DATA] {data['name']}"]

    # Location & type
    loc_parts = []
    if data.get("city") and data.get("state"):
        loc_parts.append(f"Location: {data['city']}, {data['state']}")
    elif data.get("state"):
        loc_parts.append(f"State: {data['state']}")
    ownership = _OWNERSHIP_MAP.get(data.get("ownership"))
    if ownership:
        loc_parts.append(f"Type: {ownership}")
    setting = data.get("setting")
    if setting:
        loc_parts.append(f"Setting: {setting}")
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

    # Niche grades
    grade_keys = [
        ("overall_grade", "Overall"), ("academics_grade", "Academics"),
        ("value_grade", "Value"), ("diversity_grade", "Diversity"),
        ("campus_grade", "Campus"), ("athletics_grade", "Athletics"),
        ("professors_grade", "Professors"), ("location_grade", "Location"),
        ("dorms_grade", "Dorms"), ("food_grade", "Food"),
        ("student_life_grade", "Student Life"), ("safety_grade", "Safety"),
        ("party_scene_grade", "Party Scene"),
    ]
    grade_parts = []
    for key, label in grade_keys:
        g = data.get(key)
        if g:
            grade_parts.append(f"{label}: {g}")

    niche_rank = data.get("niche_rank")
    if grade_parts:
        rank_str = f" (Rank #{niche_rank})" if niche_rank else ""
        lines.append(f"Niche Grades{rank_str}: " + " | ".join(grade_parts))

    # Niche extras
    extras = []
    rating = data.get("avg_rating")
    review_count = data.get("review_count")
    if rating:
        r_str = f"Avg Rating: {rating:.1f}/5"
        if review_count:
            r_str += f" ({review_count:,} reviews)"
        extras.append(r_str)
    rel = data.get("religious_affiliation")
    if rel and rel.lower() != "none":
        extras.append(f"Religious Affiliation: {rel}")
    greek = _pct(data.get("pct_greek_life"))
    if greek:
        extras.append(f"Greek Life: {greek}")
    on_campus = _pct(data.get("pct_students_on_campus"))
    if on_campus:
        extras.append(f"On Campus: {on_campus}")
    if extras:
        lines.append(" | ".join(extras))

    return "\n".join(lines) + "\n\n"
