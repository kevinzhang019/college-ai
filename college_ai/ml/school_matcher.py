"""Fuzzy school name matching across data sources.

Maps school names from Niche to their canonical College Scorecard UNITID.
"""

import os
import re
import json
import logging
from typing import Optional, Dict

from rapidfuzz import fuzz, process
from sqlalchemy import select

from college_ai.db.connection import get_session
from college_ai.db.models import School

logger = logging.getLogger(__name__)

# Manual overrides for names that fuzzy matching gets wrong
_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), "school_overrides.json")


def _load_overrides() -> Dict[str, int]:
    """Load manual name->UNITID overrides."""
    if os.path.exists(_OVERRIDES_PATH):
        with open(_OVERRIDES_PATH, "r") as f:
            return json.load(f)
    return {}


def _save_overrides(overrides: Dict[str, int]):
    with open(_OVERRIDES_PATH, "w") as f:
        json.dump(overrides, f, indent=2)


_CAMPUS_SUFFIX_RE = re.compile(
    r"\s*[-\u2013\u2014]\s*"
    r"(main\s+campus|central\s+campus|flagship|"
    r"all\s+campuses|global\s+campus|online)\s*$",
    re.IGNORECASE,
)


def _normalize(name: str) -> str:
    """Normalize a school name for better matching."""
    name = _CAMPUS_SUFFIX_RE.sub("", name)
    name = name.lower().strip()
    # Remove common suffixes/prefixes that hurt matching
    for noise in ["the ", " at ", " - ", ",", ".", "'"]:
        name = name.replace(noise, " ")
    # Normalize whitespace
    return " ".join(name.split())


class SchoolMatcher:
    """Matches school names to Scorecard UNITIDs using fuzzy matching."""

    def __init__(self, min_score: int = 80):
        self.min_score = min_score
        self.overrides = _load_overrides()
        self._schools: Dict[str, int] = {}  # normalized_name -> id
        self._load_schools()

    def _load_schools(self):
        session = get_session()
        try:
            schools = session.query(School.id, School.name).all()
            for sid, name in schools:
                self._schools[_normalize(name)] = sid
        finally:
            session.close()
        logger.info(f"SchoolMatcher loaded {len(self._schools)} schools")

    def match(self, name: str) -> Optional[int]:
        """Match a school name to a UNITID.

        Returns UNITID if a match is found above min_score, else None.
        """
        # Check manual overrides first
        if name in self.overrides:
            return self.overrides[name]
        if name.lower() in self.overrides:
            return self.overrides[name.lower()]

        normalized = _normalize(name)

        # Exact match
        if normalized in self._schools:
            return self._schools[normalized]

        # Fuzzy match
        if not self._schools:
            return None

        result = process.extractOne(
            normalized,
            self._schools.keys(),
            scorer=fuzz.token_sort_ratio,
            score_cutoff=self.min_score,
        )

        if result:
            matched_name, score, _ = result
            school_id = self._schools[matched_name]
            logger.debug(f"  Matched '{name}' -> '{matched_name}' (score={score})")
            return school_id

        logger.debug(f"  No match for '{name}' (best below {self.min_score})")
        return None

    def add_override(self, name: str, school_id: int):
        """Add a manual override for a school name."""
        self.overrides[name] = school_id
        _save_overrides(self.overrides)
