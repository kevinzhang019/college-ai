"""
Query Router for the College RAG system.

Handles rule-based short-circuits (greeting, essay_text, essay_prompt)
and school name extraction via fuzzy matching.

Full query classification (type, complexity, categories) is handled by
the LLM classifier in classifier.py.
"""

from __future__ import annotations

import csv
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query types
# ---------------------------------------------------------------------------

QA = "qa"
ESSAY_IDEAS = "essay_ideas"
ESSAY_REVIEW = "essay_review"
ADMISSION_PREDICTION = "admission_prediction"
RANKING = "ranking"
COMPARISON = "comparison"
GREETING = "greeting"

# ---------------------------------------------------------------------------
# Greeting patterns — short messages with no college-related content
# ---------------------------------------------------------------------------

GREETING_PATTERNS = [
    r"^(hi|hello|hey|howdy|yo|sup)\b",
    r"^good (morning|afternoon|evening|night)\b",
    r"^(thanks|thank you|thx|ty)\b",
    r"^(bye|goodbye|see you|later)\b",
    r"^how are you",
    r"^what'?s up\b",
    r"^nice to meet you",
]

# ---------------------------------------------------------------------------
# School acronym / shorthand aliases → canonical CSV name
# ---------------------------------------------------------------------------

SCHOOL_ALIASES = {
    # Pure acronyms
    "mit": "Massachusetts Institute of Technology",
    "cmu": "Carnegie Mellon University",
    "nyu": "New York University",
    "usc": "University of Southern California",
    "ucla": "University of California—Los Angeles",
    "ucsd": "University of California—San Diego",
    "uci": "University of California—Irvine",
    "ucb": "University of California—Berkeley",
    "ucd": "University of California-Davis",
    "ucf": "University of Central Florida",
    "uga": "University of Georgia",
    "uva": "University of Virginia",
    "unc": "University of North Carolina—Chapel Hill",
    "uf": "University of Florida",
    "ut": "University of Texas—Austin",
    "utsa": "University of Texas—San Antonio",
    "utd": "University of Texas—Dallas",
    "umd": "University of Maryland—College Park",
    "osu": "The Ohio State University",
    "msu": "Michigan State University",
    "lsu": "Louisiana State University",
    "fsu": "Florida State University",
    "fiu": "Florida International University",
    "byu": "Brigham Young University",
    "tcu": "Texas Christian University",
    "smu": "Southern Methodist University",
    "gwu": "George Washington University",
    "gw": "George Washington University",
    "vcu": "Virginia Commonwealth University",
    "sjsu": "San José State University",
    "sdsu": "San Diego State University",
    "asu": "Arizona State University",
    "bu": "Boston University",
    "bc": "Boston College",
    "vt": "Virginia Tech",
    "gt": "Georgia Institute of Technology",
    "uiuc": "University of Illinois Urbana-Champaign",
    "cwru": "Case Western Reserve University",
    "wfu": "Wake Forest University",
    "isu": "Iowa State University",
    "ksu": "Kansas State University",
    "ttu": "Texas Tech University",
    "unt": "University of North Texas",
    "psu": "Pennsylvania State University",
    "unh": "University of New Hampshire",
    "uvm": "University of Vermont",
    "ncsu": "North Carolina State University",
    # Shorthands / nicknames
    "umich": "University of Michigan—Ann Arbor",
    "upenn": "University of Pennsylvania",
    "washu": "Washington University in St. Louis",
    "cal poly": "Cal Poly San Luis Obispo",
    "ole miss": "University of Mississippi",
    "gatech": "Georgia Institute of Technology",
    "georgia tech": "Georgia Institute of Technology",
    "umass": "University of Massachusetts Amherst",
    "umass amherst": "University of Massachusetts Amherst",
    "umass lowell": "University of Massachusetts Lowell",
    "penn state": "Pennsylvania State University",
    "ohio state": "The Ohio State University",
    "texas a&m": "Texas A&M University",
    "a&m": "Texas A&M University",
    "cal berkeley": "University of California—Berkeley",
    "uc berkeley": "University of California—Berkeley",
    "uc davis": "University of California-Davis",
    "uc irvine": "University of California—Irvine",
    "uc san diego": "University of California—San Diego",
    "uc la": "University of California—Los Angeles",
    "iu": "Indiana University—Bloomington",
    "william and mary": "College of William & Mary",
    "william & mary": "College of William & Mary",
    "w&m": "College of William & Mary",
    "wm": "College of William & Mary",
    "u of m": "University of Michigan—Ann Arbor",
    "u of t": "University of Texas—Austin",
    "u of f": "University of Florida",
    "rutgers": "Rutgers University–New Brunswick",
    "cuny brooklyn": "CUNY Brooklyn College",
    # Single-name schools (won't fuzzy-match due to low token_sort_ratio)
    "stanford": "Stanford University",
    "harvard": "Harvard University",
    "yale": "Yale University",
    "princeton": "Princeton University",
    "dartmouth": "Dartmouth College",
    "columbia": "Columbia University",
    "cornell": "Cornell University",
    "brown": "Brown University",
    "duke": "Duke University",
    "vanderbilt": "Vanderbilt University",
    "emory": "Emory University",
    "georgetown": "Georgetown University",
    "northwestern": "Northwestern University",
    "tulane": "Tulane University",
    "fordham": "Fordham University",
    "northeastern": "Northeastern University",
    "clemson": "Clemson University",
    "auburn": "Auburn University",
    "purdue": "Purdue University",
    "baylor": "Baylor University",
    "syracuse": "Syracuse University",
    "villanova": "Villanova University",
    "howard": "Howard University",
    "rice": "Rice University",
    "babson": "Babson College",
    "bentley": "Bentley University",
    "minerva": "Minerva University",
    "binghamton": "Binghamton University",
    "clarkson": "Clarkson University",
    "notre dame": "University of Notre Dame",
}


class QueryClassification:
    """Result of the router's rule-based pre-classification.

    Only determines query_type for short-circuit cases (greeting, essay_text,
    essay_prompt). For all other queries, query_type is None and the LLM
    classifier in classifier.py determines the full classification.
    """

    __slots__ = ("query_type", "detected_schools")

    def __init__(
        self,
        query_type: Optional[str] = None,
        detected_schools: Optional[List[str]] = None,
    ):
        self.query_type = query_type
        self.detected_schools = detected_schools or []


class QueryRouter:
    """Rule-based pre-classifier and school name extractor."""

    def __init__(self):
        self._college_names = None  # lazy-loaded

    # ---- School name loading ----

    def _load_college_names(self) -> List[str]:
        """Load known college names from CSV files."""
        if self._college_names is not None:
            return self._college_names

        from pathlib import Path
        base_path = Path(__file__).parent.parent / "scraping" / "colleges"
        names = set()
        try:
            for csv_path in base_path.glob("*.csv"):
                try:
                    with open(csv_path, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            name = row.get("name", "").strip()
                            if name:
                                names.add(name)
                except Exception:
                    continue
        except Exception:
            pass

        self._college_names = sorted(names)
        return self._college_names

    # ---- School extraction ----

    MAX_SCHOOLS = 5

    @staticmethod
    def _spans_overlap(start: int, end: int, consumed: List[tuple]) -> bool:
        """Check if (start, end) overlaps any span in consumed."""
        return any(not (end <= s or start >= e) for s, e in consumed)

    def extract_schools(self, question: str) -> List[str]:
        """Extract all college names from the query text.

        Checks aliases/acronyms first, then exact substring, then fuzzy ngrams.
        Tracks consumed character spans to avoid overlapping matches.
        Deduplicates by canonical name and caps at MAX_SCHOOLS.
        """
        q_lower = question.lower()
        found = []  # type: List[str]  # canonical names, insertion order
        found_set = set()  # type: set  # lowercased canonical names for dedup
        consumed = []  # type: List[tuple]  # (start, end) character spans

        # 1. Check aliases (acronyms and shorthands) — longest first
        for alias in sorted(SCHOOL_ALIASES, key=len, reverse=True):
            if len(found) >= self.MAX_SCHOOLS:
                break
            pattern = r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])'
            m = re.search(pattern, q_lower)
            if m and not self._spans_overlap(m.start(), m.end(), consumed):
                canonical = SCHOOL_ALIASES[alias]
                if canonical.lower() not in found_set:
                    found.append(canonical)
                    found_set.add(canonical.lower())
                consumed.append((m.start(), m.end()))

        # 2. Exact substring match against known college names
        colleges = self._load_college_names()
        if colleges:
            for name in sorted(colleges, key=len, reverse=True):
                if len(found) >= self.MAX_SCHOOLS:
                    break
                name_lc = name.lower()
                idx = q_lower.find(name_lc)
                if idx != -1:
                    start, end = idx, idx + len(name_lc)
                    if not self._spans_overlap(start, end, consumed):
                        if name.lower() not in found_set:
                            found.append(name)
                            found_set.add(name.lower())
                        consumed.append((start, end))

        # 3. Fuzzy ngram match (rapidfuzz)
        if len(found) < self.MAX_SCHOOLS and colleges:
            try:
                from rapidfuzz import process as rfp, fuzz
            except ImportError:
                return found

            name_map = {n.lower(): n for n in colleges}
            words = question.split()

            # Pre-compute word start positions in the original string for
            # span tracking (match against lowercase version)
            word_starts = []  # type: List[int]
            pos = 0
            for w in words:
                idx = q_lower.find(w.lower(), pos)
                word_starts.append(idx)
                pos = idx + len(w)

            for ngram_len in range(1, min(8, len(words) + 1)):
                if len(found) >= self.MAX_SCHOOLS:
                    break
                for i in range(len(words) - ngram_len + 1):
                    if len(found) >= self.MAX_SCHOOLS:
                        break
                    ngram = " ".join(words[i:i + ngram_len])
                    span_start = word_starts[i]
                    span_end = word_starts[i + ngram_len - 1] + len(words[i + ngram_len - 1])

                    if self._spans_overlap(span_start, span_end, consumed):
                        continue

                    result = rfp.extractOne(
                        ngram.lower(),
                        name_map.keys(),
                        scorer=fuzz.token_sort_ratio,
                        score_cutoff=85,
                    )
                    if result:
                        canonical = name_map[result[0]]
                        if canonical.lower() not in found_set:
                            found.append(canonical)
                            found_set.add(canonical.lower())
                            consumed.append((span_start, span_end))

        return found

    # ---- Pre-classification ----

    def classify(
        self,
        question: str,
        essay_text: Optional[str] = None,
        essay_prompt: Optional[str] = None,
    ) -> QueryClassification:
        """Rule-based pre-classification for short-circuit cases.

        Determines query_type only for:
          - essay_text provided → essay_review
          - essay_prompt provided (no essay_text) → essay_ideas
          - greeting detected → greeting

        All other queries return query_type=None, meaning the LLM classifier
        should determine the type.

        Always extracts school names if any are detected.
        """
        detected_schools = self.extract_schools(question)

        # Essay text present → always essay_review
        if essay_text and essay_text.strip():
            return QueryClassification(ESSAY_REVIEW, detected_schools)

        # Essay prompt present (no essay text) → essay_ideas
        if essay_prompt and essay_prompt.strip():
            return QueryClassification(ESSAY_IDEAS, detected_schools)

        # Greeting detection — short messages with no substance
        if self._is_greeting(question):
            return QueryClassification(GREETING, detected_schools)

        # Everything else → LLM classifier determines type
        return QueryClassification(None, detected_schools)

    @staticmethod
    def _is_greeting(question: str) -> bool:
        """Check if the question is a greeting/off-topic short message."""
        q = question.lower().strip()
        words = q.split()
        if len(words) > 8:
            return False
        return any(re.search(p, q) for p in GREETING_PATTERNS)
