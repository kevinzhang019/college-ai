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

VALID_TYPES = {QA, ESSAY_IDEAS, ESSAY_REVIEW, ADMISSION_PREDICTION}

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


class QueryClassification:
    """Result of classifying a user query."""

    __slots__ = ("query_type", "detected_school", "confidence")

    def __init__(
        self,
        query_type: str,
        detected_school: Optional[str] = None,
        confidence: str = "rule",
    ):
        self.query_type = query_type
        self.detected_school = detected_school
        self.confidence = confidence  # "rule" or "llm"


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

        Returns the canonical school name or None.
        """
        colleges = self._load_college_names()
        if not colleges:
            return None

        try:
            from rapidfuzz import process as rfp, fuzz
        except ImportError:
            return self._extract_school_exact(question, colleges)

        # Build lowercase lookup map
        name_map = {n.lower(): n for n in colleges}

        # Try exact substring match first (cheaper)
        q_lower = question.lower()
        # Sort by length descending so "University of California—Berkeley" matches
        # before "University of California"
        for name in sorted(colleges, key=len, reverse=True):
            if name.lower() in q_lower:
                return name

        # Fuzzy match: extract best match from the question
        # We try matching progressively longer ngrams against the college list
        words = question.split()
        best_match = None
        best_score = 0

        for ngram_len in range(2, min(8, len(words) + 1)):
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
            QueryClassification with query_type, detected_school, confidence.
        """
        detected_school = self.extract_school(question)

        # If essay text is provided, it's always a review
        if essay_text and essay_text.strip():
            return QueryClassification(ESSAY_REVIEW, detected_school, "rule")

        query_type = self._classify_rules(question)
        if query_type is not None:
            return QueryClassification(query_type, detected_school, "rule")

        # LLM fallback
        query_type = self._classify_llm(question)
        return QueryClassification(query_type, detected_school, "llm")

    def _classify_rules(self, question: str) -> Optional[str]:
        """Rule-based fast path. Returns None if ambiguous."""
        q = question.lower()

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
