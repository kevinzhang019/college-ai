"""
Unified LLM query classifier for the College RAG system.

Single gpt-4.1-nano call per query that determines:
  - query_type: qa, essay_ideas, essay_review, admission_prediction, ranking
  - complexity: simple vs complex (drives model selection)
  - categories: school-data column prefixes for selective DB fetching
  - niche_categories: Niche grade categories for reranking (ranking queries only)

Falls back gracefully on any error.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---- Valid values ----

VALID_QUERY_TYPES = {
    "qa", "essay_ideas", "essay_review", "admission_prediction", "ranking",
    "comparison",
}

# Ranking categories (Niche grades — used for reranking)
RANKING_CATEGORIES = [
    "academics", "value", "diversity", "campus", "athletics",
    "party_scene", "professors", "location", "dorms", "food",
    "student_life", "safety", "other",
]

# School data categories (DB column prefixes — used for selective fetching)
SCHOOL_DATA_CATEGORIES = [
    "admissions", "student", "cost", "aid", "outcome", "institution",
]

# ---- Static system prompt (cacheable by OpenAI) ----

_CLASSIFY_SYSTEM = (
    "You classify college admissions questions. Output valid JSON only.\n\n"
    "## Fields\n"
    "- query_type: one of qa, essay_ideas, essay_review, admission_prediction, ranking\n"
    "- complexity: simple or complex\n"
    "- categories: list of relevant categories (see rules below)\n\n"
    "## Query type rules\n"
    "- qa: factual questions about colleges (admissions, programs, deadlines, tuition, campus life)\n"
    "- essay_ideas: requests for essay brainstorming, topic ideas, or essay planning help\n"
    "- essay_review: requests to review, critique, or improve an existing essay draft\n"
    "- admission_prediction: questions about chances of getting in, probability, competitiveness\n"
    "- ranking: asks for a ranking, best/worst list, or top-N schools\n"
    "  - A question about a single school is NOT a ranking.\n"
    "  - A comparison of specific named schools is a comparison, not a ranking.\n"
    "- comparison: asks to compare two or more specific named schools on one or more dimensions\n"
    "  - e.g. \"MIT vs Stanford for CS\", \"compare Duke and UNC campus life\"\n"
    "  - Must name specific schools. \"Which is better for CS?\" without naming schools is qa.\n\n"
    "## Complexity rules\n"
    "- simple: short single-topic factual lookup (e.g. \"what is MIT's acceptance rate?\")\n"
    "- complex: everything else (comparisons, strategy, multi-part, essays, predictions)\n\n"
    "## Category rules\n"
    "categories: school data categories — pick from:\n"
    "  admissions (test scores, acceptance rate, requirements)\n"
    "  student (enrollment, demographics, retention, faculty ratio)\n"
    "  cost (tuition, net price, cost of attendance)\n"
    "  aid (financial aid, scholarships, debt, loans)\n"
    "  outcome (graduation rate, earnings after graduation)\n"
    "  institution (endowment, faculty quality, spending per student)\n"
    "  - Pick ALL that apply. If none are relevant, output an empty list.\n"
    "  - These apply to ALL query types including ranking.\n\n"
    "niche_categories: ONLY when query_type is \"ranking\", pick from:\n"
    "  academics, value, diversity, campus, athletics, party_scene, "
    "professors, location, dorms, food, student_life, safety, other\n"
    "  - Pick ALL that apply. \"other\" means no named category fits.\n"
    "  - For non-ranking queries, output an empty list.\n\n"
    "## Output format\n"
    'Output ONLY valid JSON: {"query_type": "...", "complexity": "...", "categories": [...], "niche_categories": [...]}'
)


class QueryIntent:
    """Result of classifying a user query."""

    __slots__ = ("query_type", "complexity", "categories", "niche_categories")

    def __init__(
        self,
        query_type: str = "qa",
        complexity: str = "complex",
        categories: Optional[List[str]] = None,
        niche_categories: Optional[List[str]] = None,
    ):
        self.query_type = query_type
        self.complexity = complexity
        self.categories = categories or []
        self.niche_categories = niche_categories or []

    def __repr__(self) -> str:
        return (
            f"QueryIntent(query_type={self.query_type!r}, "
            f"complexity={self.complexity!r}, "
            f"categories={self.categories}, "
            f"niche_categories={self.niche_categories})"
        )


# Module-level OpenAI client (lazy)
_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY", "").strip().strip('"').strip("'")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        _client = OpenAI(api_key=api_key)
    return _client


def classify_query(question: str) -> QueryIntent:
    """Classify a user query into type, complexity, and categories.

    Uses gpt-4.1-nano for classification. Returns a safe default
    (``query_type="qa"``, ``complexity="complex"``) on any error.
    """
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=80,
            prompt_cache_key="cole-classify",
        )

        if not response or not response.choices:
            return QueryIntent()

        raw = response.choices[0].message.content or ""
        raw = raw.strip()

        # Strip markdown code fences if the model wraps its output
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        parsed = json.loads(raw)

        # Validate query_type
        query_type = parsed.get("query_type", "qa")
        if query_type not in VALID_QUERY_TYPES:
            query_type = "qa"

        # Validate complexity
        complexity = parsed.get("complexity", "complex")
        if complexity not in ("simple", "complex"):
            complexity = "complex"

        # Validate categories (school data prefixes — all query types)
        raw_categories = parsed.get("categories", [])
        valid_data_set = set(SCHOOL_DATA_CATEGORIES)
        categories = [c for c in raw_categories if c in valid_data_set]

        # Validate niche_categories (ranking queries only)
        niche_categories = []  # type: List[str]
        if query_type == "ranking":
            raw_niche = parsed.get("niche_categories", [])
            valid_niche_set = set(RANKING_CATEGORIES)
            niche_categories = [c for c in raw_niche if c in valid_niche_set]
            if not niche_categories:
                niche_categories = ["academics"]

        intent = QueryIntent(
            query_type=query_type,
            complexity=complexity,
            categories=categories,
            niche_categories=niche_categories,
        )
        logger.info("Query classified: %s for query=%r", intent, question[:80])
        return intent

    except Exception:
        logger.debug("Query classification failed, using defaults", exc_info=True)
        return QueryIntent()
