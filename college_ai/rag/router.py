"""
Query Router for the College RAG system.

Two-layer classifier:
  Layer 1 — Rule-based fast path (zero latency, handles ~85% of queries)
  Layer 2 — LLM fallback for ambiguous cases

Also extracts school names from query text via fuzzy matching.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from typing import List, Optional

from college_ai.rag.prompts import CLASSIFY_SYSTEM, CLASSIFY_USER

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query types
# ---------------------------------------------------------------------------

QA = "qa"
ESSAY_IDEAS = "essay_ideas"
ESSAY_REVIEW = "essay_review"
ADMISSION_PREDICTION = "admission_prediction"
GREETING = "greeting"

VALID_TYPES = {QA, ESSAY_IDEAS, ESSAY_REVIEW, ADMISSION_PREDICTION, GREETING}

# ---------------------------------------------------------------------------
# Signal keyword lists
# ---------------------------------------------------------------------------

ESSAY_SIGNALS = [
    "essay", "common app", "personal statement", "why school", "why college",
    "why i want to attend", "supplement", "supplemental", "write about",
    "help me write", "draft", "review my", "feedback on", "edit my",
    "brainstorm", "topic ideas", "activities essay", "additional information",
    "coalition app", "uc essay", "uc prompt", "why us",
]

ESSAY_REVIEW_SIGNALS = [
    "review my", "feedback on", "edit my", "critique my", "improve my",
    "look at my", "check my", "here is my essay", "here's my essay",
    "read my", "what do you think of my",
]

FACTUAL_SIGNALS = [
    "acceptance rate", "gpa", "sat", "act", "deadline", "tuition",
    "financial aid", "fafsa", "scholarship", "major", "program",
    "apply", "requirements", "rolling admission", "early decision",
    "early action", "regular decision", "cost of attendance",
    "student faculty ratio", "campus", "dorm", "housing",
    "residence hall", "dining", "meal plan", "class size",
    "professor", "research", "internship", "career",
    "graduation rate", "retention rate", "ranking",
    "location", "weather", "greek life", "clubs",
    "study abroad", "sports", "athletics", "division",
    "net price", "sticker price", "merit aid", "need-based",
    "css profile", "demonstrated interest", "yield rate",
    "waitlist", "ap credit", "course selection",
]

# Reuse patterns from bridge.py
ADMISSION_PREDICTION_PATTERNS = [
    r"what are my chances",
    r"can i get into",
    r"will i get into",
    r"do i have a chance",
    r"chances of getting in",
    r"admission probability",
    r"how likely am i",
    r"likelihood of acceptance",
    r"chance of admission",
    r"get accepted",
    r"admitted to",
    r"acceptance rate.*gpa|gpa.*acceptance rate",
    r"sat.*chance|chance.*sat",
    r"gpa.*\d+\.\d+.*chance|chance.*gpa.*\d+\.\d+",
]


# Greeting / off-topic patterns — short messages with no college-related content
GREETING_PATTERNS = [
    r"^(hi|hello|hey|howdy|yo|sup)\b",
    r"^good (morning|afternoon|evening|night)\b",
    r"^(thanks|thank you|thx|ty)\b",
    r"^(bye|goodbye|see you|later)\b",
    r"^how are you",
    r"^what'?s up\b",
    r"^nice to meet you",
]

SIMPLE = "simple"
COMPLEX = "complex"

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

# Keywords that indicate a complex (non-lookup) question
COMPLEX_SIGNALS = [
    "compare", "versus", "vs", "difference between",
    "how do i", "how to", "should i", "best way", "strategy",
    "steps to", "process for", "what should", "recommend",
    "worth it", "better", "pros and cons", "trade-off",
    "explain", "why does", "why is", "what makes",
]


class QueryClassification:
    """Result of classifying a user query."""

    __slots__ = ("query_type", "detected_school", "confidence", "complexity")

    def __init__(
        self,
        query_type: str,
        detected_school: Optional[str] = None,
        confidence: str = "rule",
        complexity: str = COMPLEX,
    ):
        self.query_type = query_type
        self.detected_school = detected_school
        self.confidence = confidence  # "rule" or "llm"
        self.complexity = complexity  # "simple" or "complex"


class QueryRouter:
    """Classifies queries and extracts school names."""

    def __init__(self):
        self._college_names = None  # lazy-loaded
        self._openai_client = None

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

    def extract_school(self, question: str) -> Optional[str]:
        """Extract a college name from the query text via fuzzy matching.

        Checks aliases/acronyms first, then exact substring, then fuzzy ngrams.
        Returns the canonical school name or None.
        """
        # 1. Check aliases (acronyms and shorthands) — longest first
        q_lower = question.lower()
        # Build word list for token-boundary matching
        q_words = re.findall(r"[a-z&']+(?:\s+[a-z&']+)*", q_lower)
        q_joined = " ".join(q_words)

        for alias in sorted(SCHOOL_ALIASES, key=len, reverse=True):
            # Use word-boundary matching to avoid false positives
            # e.g., "bu" shouldn't match inside "about"
            pattern = r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])'
            if re.search(pattern, q_lower):
                return SCHOOL_ALIASES[alias]

        # 2. Exact substring match against known college names
        colleges = self._load_college_names()
        if not colleges:
            return None

        for name in sorted(colleges, key=len, reverse=True):
            if name.lower() in q_lower:
                return name

        # 3. Fuzzy ngram match (rapidfuzz)
        try:
            from rapidfuzz import process as rfp, fuzz
        except ImportError:
            return None

        name_map = {n.lower(): n for n in colleges}
        words = question.split()
        best_match = None
        best_score = 0

        for ngram_len in range(1, min(8, len(words) + 1)):
            for i in range(len(words) - ngram_len + 1):
                ngram = " ".join(words[i:i + ngram_len])
                result = rfp.extractOne(
                    ngram.lower(),
                    name_map.keys(),
                    scorer=fuzz.token_sort_ratio,
                    score_cutoff=85,
                )
                if result and result[1] > best_score:
                    best_score = result[1]
                    best_match = name_map[result[0]]

        return best_match

    @staticmethod
    def _extract_school_exact(
        question: str, colleges: List[str]
    ) -> Optional[str]:
        """Fallback: exact substring match when rapidfuzz is unavailable."""
        q_lower = question.lower()
        for name in sorted(colleges, key=len, reverse=True):
            if name.lower() in q_lower:
                return name
        return None

    # ---- Classification ----

    def classify(
        self,
        question: str,
        essay_text: Optional[str] = None,
    ) -> QueryClassification:
        """Classify a query into a type and optionally extract a school name.

        Args:
            question: The user's question text.
            essay_text: Optional pasted essay draft. If provided, forces essay_review mode.

        Returns:
            QueryClassification with query_type, detected_school, confidence, complexity.
        """
        detected_school = self.extract_school(question)

        # If essay text is provided, it's always a review
        if essay_text and essay_text.strip():
            return QueryClassification(ESSAY_REVIEW, detected_school, "rule", COMPLEX)

        query_type = self._classify_rules(question)
        if query_type is not None:
            complexity = self._classify_complexity(question, query_type)
            return QueryClassification(query_type, detected_school, "rule", complexity)

        # LLM fallback — always treat as complex (ambiguous query)
        query_type = self._classify_llm(question)
        return QueryClassification(query_type, detected_school, "llm", COMPLEX)

    def _classify_rules(self, question: str) -> Optional[str]:
        """Rule-based fast path. Returns None if ambiguous."""
        q = question.lower().strip()

        # Greetings / off-topic: short messages with no college-related signals
        words = q.split()
        if len(words) <= 8:
            essay_count = sum(1 for s in ESSAY_SIGNALS if s in q)
            factual_count = sum(1 for s in FACTUAL_SIGNALS if s in q)
            if essay_count == 0 and factual_count == 0:
                if any(re.search(p, q) for p in GREETING_PATTERNS):
                    return GREETING

        # Check admission prediction first (specific patterns)
        if any(re.search(p, q) for p in ADMISSION_PREDICTION_PATTERNS):
            return ADMISSION_PREDICTION

        # Score essay vs factual signals
        essay_score = sum(1 for s in ESSAY_SIGNALS if s in q)
        review_score = sum(1 for s in ESSAY_REVIEW_SIGNALS if s in q)
        factual_score = sum(1 for s in FACTUAL_SIGNALS if s in q)

        # Essay review takes priority over generic essay
        if review_score >= 1 and review_score >= factual_score:
            return ESSAY_REVIEW
        if essay_score > factual_score and essay_score >= 1:
            return ESSAY_IDEAS
        if factual_score > essay_score and factual_score >= 1:
            return QA

        return None  # ambiguous → LLM fallback

    @staticmethod
    def _classify_complexity(question: str, query_type: str) -> str:
        """Determine query complexity for model routing.

        Only Q&A queries can be classified as simple.  All other types
        (essay_ideas, essay_review, admission_prediction) are always complex.

        A Q&A query is simple when ALL of these hold:
          - Short (< 20 words)
          - No comparison/strategy keywords
          - At most 1 factual signal (single-topic lookup)
        """
        if query_type != QA:
            return COMPLEX

        q = question.lower()

        # Long questions are complex
        if len(question.split()) >= 20:
            return COMPLEX

        # Comparison / strategy keywords → complex
        if any(s in q for s in COMPLEX_SIGNALS):
            return COMPLEX

        # Multiple factual signals means multi-part → complex
        factual_count = sum(1 for s in FACTUAL_SIGNALS if s in q)
        if factual_count > 1:
            return COMPLEX

        return SIMPLE

    def _classify_llm(self, question: str) -> str:
        """LLM fallback for ambiguous queries."""
        try:
            client = self._get_openai_client()
            response = client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": CLASSIFY_SYSTEM},
                    {
                        "role": "user",
                        "content": CLASSIFY_USER.format(question=question),
                    },
                ],
                temperature=0,
                max_tokens=10,
            )
            if response and response.choices:
                result = response.choices[0].message.content or ""
                result = result.strip().lower().replace('"', "").replace("'", "")
                if result in VALID_TYPES:
                    return result
        except Exception as exc:
            logger.debug("LLM classification failed: %s", exc)

        # Default to QA if everything fails
        return QA

    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import OpenAI

            api_key = os.getenv("OPENAI_API_KEY", "").strip().strip('"').strip("'")
            self._openai_client = OpenAI(api_key=api_key)
        return self._openai_client
