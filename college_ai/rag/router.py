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
from typing import Dict, List, Optional

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
# Greeting patterns - short messages with no college-related content
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
    "ucla": "University of California - Los Angeles",
    "ucsd": "University of California - San Diego",
    "uci": "University of California - Irvine",
    "ucb": "University of California - Berkeley",
    "ucd": "University of California - Davis",
    "ucf": "University of Central Florida",
    "uga": "University of Georgia",
    "uva": "University of Virginia",
    "unc": "University of North Carolina at Chapel Hill",
    "uf": "University of Florida",
    "ut": "University of Texas - Austin",
    "utsa": "The University of Texas at San Antonio",
    "utd": "University of Texas - Dallas",
    "umd": "University of Maryland - College Park",
    "osu": "Ohio State University",
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
    "sjsu": "San Jose State University",
    "sdsu": "San Diego State University",
    "asu": "Arizona State University",
    "bu": "Boston University",
    "bc": "Boston College",
    "vt": "Virginia Tech",
    "gt": "Georgia Institute of Technology",
    "uiuc": "University of Illinois - Urbana-Champaign",
    "cwru": "Case Western Reserve University",
    "wfu": "Wake Forest University",
    "isu": "Iowa State University",
    "ttu": "Texas Tech University",
    "unt": "University of North Texas",
    "psu": "Pennsylvania State University",
    "unh": "University of New Hampshire",
    "uvm": "University of Vermont",
    "ncsu": "North Carolina State University",
    # Shorthands / nicknames
    "umich": "University of Michigan - Ann Arbor",
    "upenn": "University of Pennsylvania",
    "washu": "Washington University in St. Louis",
    "cal poly": "Cal Poly",
    "ole miss": "University of Mississippi",
    "gatech": "Georgia Institute of Technology",
    "georgia tech": "Georgia Institute of Technology",
    "umass": "University of Massachusetts - Amherst",
    "umass amherst": "University of Massachusetts - Amherst",
    "umass lowell": "University of Massachusetts - Lowell",
    "penn state": "Pennsylvania State University",
    "ohio state": "Ohio State University",
    "texas a&m": "Texas A and M University",
    "a&m": "Texas A and M University",
    "cal berkeley": "University of California - Berkeley",
    "uc berkeley": "University of California - Berkeley",
    "uc davis": "University of California - Davis",
    "uc irvine": "University of California - Irvine",
    "uc san diego": "University of California - San Diego",
    "uc la": "University of California - Los Angeles",
    "iu": "Indiana University - Bloomington",
    "william and mary": "William and Mary",
    "william & mary": "William and Mary",
    "w&m": "William and Mary",
    "wm": "William and Mary",
    "u of m": "University of Michigan - Ann Arbor",
    "u of t": "University of Texas - Austin",
    "u of f": "University of Florida",
    "rutgers": "Rutgers University - New Brunswick",
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


# ---------------------------------------------------------------------------
# Flagship aliases — stripped "University of X" → canonical full name.
# ---------------------------------------------------------------------------
#
# For university systems where one campus is clearly the flagship, map the
# bare "University of <State>" form to the official canonical name used by
# Turso. This lets queries like "tell me about university of michigan" or
# "univ of mich" (after shorthand expansion) resolve to the Ann Arbor
# campus instead of fuzzy-matching Michigan State University by accident.
#
# Systems with peer campuses (University of California, University of
# Nevada, University of Alaska) are intentionally excluded — there's no
# single "obvious" flagship. Systems whose flagship is already stored
# without a suffix in the CSV (University of Alabama, Arkansas, Houston,
# Missouri, South Carolina, Washington, Cincinnati) don't need a mapping
# because the substring scan already resolves them.
FLAGSHIP_ALIASES = {
    "university of colorado":       "University of Colorado - Boulder",
    "university of hawaii":         "University of Hawaii at Manoa",
    "university of illinois":       "University of Illinois - Urbana-Champaign",
    "university of louisiana":      "University of Louisiana at Lafayette",
    "university of maryland":       "University of Maryland - College Park",
    "university of massachusetts":  "University of Massachusetts - Amherst",
    "university of michigan":       "University of Michigan - Ann Arbor",
    "university of minnesota":      "University of Minnesota - Twin Cities",
    "university of nebraska":       "University of Nebraska - Lincoln",
    "university of north carolina": "University of North Carolina at Chapel Hill",
    "university of tennessee":      "University of Tennessee - Knoxville",
    "university of texas":          "University of Texas - Austin",
    "university of wisconsin":      "University of Wisconsin - Madison",
}


# ---------------------------------------------------------------------------
# Shorthand expansion (second-pass school detection)
# ---------------------------------------------------------------------------
#
# Applied to a copy of the user query before the second extraction pass.
# Lets queries like "U of CA Berkeley" or "tell me about bama" hit the
# same matcher as their fully-spelled forms.

# Phase 1 — word expansions (state-name abbreviations).
# Applied first so "ariz state" becomes "arizona state" before
# Phase 2 turns it into "arizona state university".
_SHORTHAND_WORDS = {
    " ariz ": " arizona ",
    " cal ":  " california ",
    " cali ": " california ",
    " colo ": " colorado ",
    " col ":  " colorado ",
    " conn ": " connecticut ",
    " del ":  " delaware ",
    " mass ": " massachusetts ",
    " mich ": " michigan ",
    " minn ": " minnesota ",
    " neb ":  " nebraska ",
    " okla ": " oklahoma ",
    " penn ": " pennsylvania ",
    " tenn ": " tennessee ",
    " tex ":  " texas ",
    " wisc ": " wisconsin ",
}

# Phase 2 — structural expansions (abbreviations for "university",
# "university of", "state university"). Applied after Phase 1 so
# combined shorthands like "U mich" or "ariz state" resolve fully.
_SHORTHAND_STRUCTURAL = {
    " u of ":  " university of ",
    " u ":     " university of ",
    " uni ":   " university ",
    " univ ":  " university ",
    " state ": " state university ",
}

# Uppercase-only state codes, matched on the original-case query so we
# don't fire on common lowercase words like "or", "in", "me", "hi", "ok".
_STATE_CODES_UPPER = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# Lowercase state codes — 43 of 50. Only 7 are excluded because they
# collide with extremely common English words: or, in, me, hi, ok,
# id, la. The remaining collisions (oh, pa, ma, md, de, il, etc.) are
# accepted as noise — bare state names rarely substring-match a
# canonical college name, so most firings are silent no-ops anyway.
_STATE_CODES_LOWER = {
    "ak": "Alaska", "al": "Alabama", "ar": "Arkansas", "az": "Arizona",
    "ca": "California", "co": "Colorado", "ct": "Connecticut",
    "de": "Delaware", "fl": "Florida", "ga": "Georgia",
    "ia": "Iowa", "il": "Illinois", "ks": "Kansas", "ky": "Kentucky",
    "ma": "Massachusetts", "md": "Maryland", "mi": "Michigan",
    "mn": "Minnesota", "mo": "Missouri", "ms": "Mississippi",
    "mt": "Montana", "nc": "North Carolina", "nd": "North Dakota",
    "ne": "Nebraska", "nh": "New Hampshire", "nj": "New Jersey",
    "nm": "New Mexico", "nv": "Nevada", "ny": "New York",
    "oh": "Ohio", "pa": "Pennsylvania", "ri": "Rhode Island",
    "sc": "South Carolina", "sd": "South Dakota",
    "tn": "Tennessee", "tx": "Texas", "ut": "Utah",
    "va": "Virginia", "vt": "Vermont", "wa": "Washington",
    "wi": "Wisconsin", "wv": "West Virginia", "wy": "Wyoming",
}

# State code regexes use space-based lookaround assertions: they require
# a space on each side (just like shorthand expansions) but use zero-width
# lookbehind/lookahead so the spaces are NOT consumed. This avoids shared-
# space conflicts between adjacent codes (e.g. " CA TX " → both match).
# The query is padded with leading/trailing spaces, so start/end codes work.
_STATE_UPPER_RE = re.compile(
    r"(?<= )(" + "|".join(_STATE_CODES_UPPER.keys()) + r")(?= )"
)
_STATE_LOWER_RE = re.compile(
    r"(?<= )(" + "|".join(_STATE_CODES_LOWER.keys()) + r")(?= )"
)
def _build_shorthand_re(table):
    """Compile a case-insensitive alternation regex from a shorthand table,
    sorted longest-first so " u of " wins over " u " at the same position."""
    keys = sorted(table.keys(), key=len, reverse=True)
    return re.compile(
        "(" + "|".join(re.escape(k) for k in keys) + ")",
        re.IGNORECASE,
    )


_SHORTHAND_WORDS_RE = _build_shorthand_re(_SHORTHAND_WORDS)
_SHORTHAND_STRUCTURAL_RE = _build_shorthand_re(_SHORTHAND_STRUCTURAL)


def expand_query_shorthand(text: str) -> str:
    """Expand common school-related shorthands so a second extraction
    pass can detect schools that the original-text pass missed.

    Two phases (both case-insensitive, whitespace-bounded):
      1. Word expansions (bama → alabama, mich → michigan, ...)
      2. Structural expansions (u → university of, state → state university)

    The split ensures adjacent shorthands like "ariz state" or "U mich"
    resolve fully: phase 1 produces "arizona state", phase 2 produces
    "arizona state university".

    Returns the expanded text (stripped), or the original if nothing matched.
    """
    padded = " " + text + " "

    padded = _SHORTHAND_WORDS_RE.sub(
        lambda m: _SHORTHAND_WORDS[m.group(0).lower()], padded
    )
    padded = _SHORTHAND_STRUCTURAL_RE.sub(
        lambda m: _SHORTHAND_STRUCTURAL[m.group(0).lower()], padded
    )
    padded = _STATE_UPPER_RE.sub(
        lambda m: _STATE_CODES_UPPER[m.group(1)], padded
    )
    padded = _STATE_LOWER_RE.sub(
        lambda m: _STATE_CODES_LOWER[m.group(1)], padded
    )

    return padded.strip()


_PUNCT_RE = re.compile(r"[^a-zA-Z0-9 ]+")
_MULTI_SPACE_RE = re.compile(r" {2,}")


def _strip_punctuation(text: str) -> str:
    """Replace non-alphanumeric/non-space characters with spaces, collapse runs."""
    return _MULTI_SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", text)).strip()


def _load_db_aliases() -> Dict[str, str]:
    """Load school aliases from the top 1000 schools by student size in Turso."""
    from college_ai.db.connection import get_session
    from college_ai.db.models import School

    session = get_session()
    try:
        rows = session.query(School.name, School.identity_alias).filter(
            School.identity_alias.isnot(None),
            School.identity_alias != '',
        ).order_by(School.student_size.desc()).limit(1000).all()
    finally:
        session.close()

    aliases = {}  # type: Dict[str, str]
    for canonical_name, alias_str in rows:
        for alias in alias_str.split(','):
            alias = alias.strip()
            if not alias:
                continue
            key = alias.lower()
            if key not in aliases:
                aliases[key] = canonical_name
    return aliases


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
        self._college_names_stripped = None  # type: Optional[Dict[str, str]]
        self._merged_aliases = None  # type: Optional[Dict[str, str]]
        self._alias_pattern = None  # type: Optional[re.Pattern]

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

    def _get_college_names_stripped(self) -> Dict[str, str]:
        """Stripped college name → canonical name mapping for second-pass
        substring and fuzzy matching.  Punctuation is replaced with spaces
        so "Texas A&M University" becomes "Texas A M University" and can
        match a query that has also been punctuation-stripped."""
        if self._college_names_stripped is not None:
            return self._college_names_stripped

        mapping = {}  # type: Dict[str, str]
        for name in self._load_college_names():
            key = _strip_punctuation(name)
            if key not in mapping:
                mapping[key] = name
        self._college_names_stripped = mapping
        return mapping

    # ---- Alias loading ----

    def _get_merged_aliases(self) -> Dict[str, str]:
        """Merge hardcoded SCHOOL_ALIASES + FLAGSHIP_ALIASES with DB aliases.

        Priority: SCHOOL_ALIASES > FLAGSHIP_ALIASES > DB aliases. Flagship
        entries beat DB values so a stray DB alias can't silently redirect
        "university of michigan" away from Ann Arbor.
        """
        if self._merged_aliases is not None:
            return self._merged_aliases

        merged = dict(SCHOOL_ALIASES)
        for key, canonical in FLAGSHIP_ALIASES.items():
            if key not in merged:
                merged[key] = canonical
        try:
            db_aliases = _load_db_aliases()
            for key, canonical in db_aliases.items():
                if key not in merged:
                    merged[key] = canonical
        except Exception:
            logger.warning("Failed to load DB aliases, using hardcoded only", exc_info=True)

        self._merged_aliases = merged
        logger.info(
            "Merged aliases: %d hardcoded + %d flagship + %d from DB = %d total",
            len(SCHOOL_ALIASES),
            len(FLAGSHIP_ALIASES),
            len(merged) - len(SCHOOL_ALIASES) - len(FLAGSHIP_ALIASES),
            len(merged),
        )
        return merged

    def _get_alias_pattern(self) -> re.Pattern:
        """Compile a single alternation regex from all merged aliases."""
        if self._alias_pattern is not None:
            return self._alias_pattern

        aliases = self._get_merged_aliases()
        sorted_aliases = sorted(aliases.keys(), key=len, reverse=True)
        pattern = r'(?<![a-z])(' + '|'.join(re.escape(a) for a in sorted_aliases) + r')(?![a-z])'
        self._alias_pattern = re.compile(pattern)
        return self._alias_pattern

    # ---- School extraction ----

    MAX_SCHOOLS = 5

    @staticmethod
    def _spans_overlap(start: int, end: int, consumed: List[tuple]) -> bool:
        """Check if (start, end) overlaps any span in consumed."""
        return any(not (end <= s or start >= e) for s, e in consumed)

    def extract_schools(self, question: str) -> List[str]:
        """Extract all college names from the query text.

        Runs the matcher twice: once on the raw question, then (if under
        the cap) once on a punctuation-stripped + shorthand-expanded copy.
        Stripping happens *before* expansion so shorthands adjacent to
        punctuation (e.g. "bama's") can fire.  The second pass also uses
        stripped college names for consistent comparison (so "Texas A&M
        University" matches "texas a m university" in the stripped query).
        Results are merged deduplicated by canonical name and capped at
        MAX_SCHOOLS.
        """
        found = self._match_schools_in_text(question)
        if len(found) >= self.MAX_SCHOOLS:
            return found

        expanded = expand_query_shorthand(_strip_punctuation(question))
        if expanded == question:
            return found

        second = self._match_schools_in_text(expanded, strip_punct=True)
        seen = {s.lower() for s in found}
        for school in second:
            key = school.lower()
            if key in seen:
                continue
            found.append(school)
            seen.add(key)
            if len(found) >= self.MAX_SCHOOLS:
                break
        return found

    def _match_schools_in_text(
        self, question: str, strip_punct: bool = False,
    ) -> List[str]:
        """Single matching pass: aliases → exact substring → fuzzy ngrams.

        Tracks consumed character spans to avoid overlapping matches and
        caps results at MAX_SCHOOLS.

        When *strip_punct* is True (second pass), the substring and fuzzy
        stages compare against punctuation-stripped college names so that
        queries like "texas a m university" match "Texas A&M University".
        """
        q_lower = question.lower()
        found = []  # type: List[str]  # canonical names, insertion order
        found_set = set()  # type: set  # lowercased canonical names for dedup
        consumed = []  # type: List[tuple]  # (start, end) character spans

        # 1. Check all aliases (hardcoded + DB) via single compiled regex.
        #    Alias keys with punctuation (a&m, w&m, st.) may not match on
        #    a stripped query, but those are caught by the first pass on the
        #    original text.
        aliases = self._get_merged_aliases()
        alias_pattern = self._get_alias_pattern()

        for m in alias_pattern.finditer(q_lower):
            if len(found) >= self.MAX_SCHOOLS:
                break
            if not self._spans_overlap(m.start(), m.end(), consumed):
                canonical = aliases[m.group()]
                if canonical.lower() not in found_set:
                    found.append(canonical)
                    found_set.add(canonical.lower())
                consumed.append((m.start(), m.end()))

        # Build scan pairs: (lowercased_scan_name, canonical_name).
        # When strip_punct is True, scan names are stripped of punctuation
        # so "Texas A&M University" becomes "texas a m university".
        if strip_punct:
            stripped_map = self._get_college_names_stripped()
            scan_pairs = [(k.lower(), v) for k, v in stripped_map.items()]
        else:
            colleges = self._load_college_names()
            scan_pairs = [(n.lower(), n) for n in colleges] if colleges else []

        # 2. Exact substring match against known college names
        if scan_pairs:
            for scan_lc, canonical in sorted(scan_pairs, key=lambda x: len(x[0]), reverse=True):
                if len(found) >= self.MAX_SCHOOLS:
                    break
                idx = q_lower.find(scan_lc)
                if idx != -1:
                    start, end = idx, idx + len(scan_lc)
                    if not self._spans_overlap(start, end, consumed):
                        if canonical.lower() not in found_set:
                            found.append(canonical)
                            found_set.add(canonical.lower())
                        consumed.append((start, end))

        # 3. Fuzzy ngram match (rapidfuzz)
        if len(found) < self.MAX_SCHOOLS and scan_pairs:
            try:
                from rapidfuzz import process as rfp, fuzz
            except ImportError:
                return found

            name_map = {scan_lc: canonical for scan_lc, canonical in scan_pairs}
            words = question.split()

            # Pre-compute word start positions in the original string for
            # span tracking (match against lowercase version)
            word_starts = []  # type: List[int]
            pos = 0
            for w in words:
                idx = q_lower.find(w.lower(), pos)
                word_starts.append(idx)
                pos = idx + len(w)

            # Iterate longest-first so that "university of california berkeley"
            # wins over a spurious 2-word fuzzy hit like "is university" →
            # "Lewis University". Matches the longest-first ordering used
            # by the alias and exact-substring stages above.
            for ngram_len in range(min(7, len(words)), 0, -1):
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
