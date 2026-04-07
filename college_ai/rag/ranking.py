"""
LLM-based ranking intent detection for the College RAG system.

Uses gpt-4.1-nano to classify whether a question is a ranking query and
which Niche categories it maps to.  Falls back gracefully on any error.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List

logger = logging.getLogger(__name__)

RANKING_CATEGORIES = [
    "academics", "value", "diversity", "campus", "athletics",
    "party_scene", "professors", "location", "dorms", "food",
    "student_life", "safety", "other",
]

# Static system prompt — cacheable by OpenAI.
_RANKING_SYSTEM = (
    "You classify college admissions questions.\n"
    "Determine whether the question is asking for a ranking, comparison by "
    "quality, or \"best/worst\" list of schools.\n\n"
    "Categories (pick ALL that apply):\n"
    "academics, value, diversity, campus, athletics, party_scene, "
    "professors, location, dorms, food, student_life, safety, other\n\n"
    "Rules:\n"
    "- If the query is NOT a ranking question, output: "
    '{\"is_ranking\": false, \"categories\": []}\n'
    "- If it IS a ranking question, output: "
    '{\"is_ranking\": true, \"categories\": [\"...\"]}\n'
    '- "other" means the ranking intent does not fit any named category.\n'
    "- A question about a single school (\"tell me about MIT\") is NOT a ranking.\n"
    "- A comparison of two specific schools (\"compare MIT vs Stanford\") is NOT a ranking.\n"
    "- Output valid JSON only, nothing else."
)


class RankingIntent:
    """Result of ranking intent detection."""

    __slots__ = ("is_ranking", "categories")

    def __init__(self, is_ranking: bool = False, categories: List[str] = None):
        self.is_ranking = is_ranking
        self.categories = categories or []

    def __repr__(self) -> str:
        return f"RankingIntent(is_ranking={self.is_ranking}, categories={self.categories})"


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


def detect_ranking_intent(question: str) -> RankingIntent:
    """Detect whether *question* is a ranking query and which categories apply.

    Uses gpt-4.1-nano for classification.  Returns a safe default
    (``is_ranking=False``) on any error.
    """
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": _RANKING_SYSTEM},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=60,
        )

        if not response or not response.choices:
            return RankingIntent()

        raw = response.choices[0].message.content or ""
        raw = raw.strip()

        # Strip markdown code fences if the model wraps its output
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        parsed = json.loads(raw)
        is_ranking = bool(parsed.get("is_ranking", False))

        if not is_ranking:
            return RankingIntent()

        categories = parsed.get("categories", [])
        # Validate categories
        valid = [c for c in categories if c in RANKING_CATEGORIES]
        if not valid:
            valid = ["academics"]

        intent = RankingIntent(is_ranking=True, categories=valid)
        logger.info("Ranking intent detected: %s for query=%r", intent, question[:80])
        return intent

    except Exception:
        logger.debug("Ranking detection failed, defaulting to non-ranking", exc_info=True)
        return RankingIntent()
