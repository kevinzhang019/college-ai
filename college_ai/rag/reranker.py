"""
Cross-encoder reranking for the College RAG system.

Primary: Cohere rerank-v3.5 (requires COHERE_API_KEY env var).
Fallback: passthrough (returns candidates in their original order).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Letter grade → numeric for ranking boost
_GRADE_TO_NUM = {
    "A+": 4.3, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F": 0.0,
}

# Maps ranking category → school_data dict key for the Niche grade
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


class Reranker:
    """Reranks retrieved candidates using Cohere cross-encoder.

    Gracefully degrades to passthrough if Cohere is unavailable.
    """

    def __init__(self):
        self._cohere_client = None
        self._available = None  # tri-state: None = unchecked

    @property
    def available(self) -> bool:
        """Whether Cohere reranking is available."""
        self._init_cohere()
        return bool(self._available)

    def _init_cohere(self) -> bool:
        """Lazily initialize the Cohere client. Returns True if available."""
        if self._available is not None:
            return self._available

        api_key = os.getenv("COHERE_API_KEY", "").strip()
        if not api_key:
            logger.info("COHERE_API_KEY not set — reranking disabled, using retrieval order.")
            self._available = False
            return False

        try:
            import cohere
            self._cohere_client = cohere.ClientV2(api_key=api_key)
            self._available = True
            logger.info("Cohere reranker initialized (rerank-v3.5).")
            return True
        except ImportError:
            logger.warning("cohere package not installed — reranking disabled.")
            self._available = False
            return False
        except Exception as exc:
            logger.warning("Failed to init Cohere client: %s", exc)
            self._available = False
            return False

    def rerank(
        self,
        query: str,
        hits: List[Dict[str, Any]],
        top_k: int = 8,
        ranking_intent: Optional[Any] = None,
        school_data_map: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Rerank hits by relevance to the query.

        Args:
            query: The user's question text.
            hits: Candidate documents from hybrid retrieval.
            top_k: Number of top results to return.
            ranking_intent: Optional RankingIntent with is_ranking and categories.
            school_data_map: Optional dict mapping lowercased school name → school data dict.

        Returns:
            Reranked list of hits, truncated to top_k.
        """
        if len(hits) <= 1:
            return hits

        if not self._init_cohere():
            return hits[:top_k]

        try:
            # Build document strings for Cohere (rerank-v3.5 supports ~4096 tokens;
            # use up to 3000 chars to cover full chunk content)
            documents = []
            for h in hits:
                title = h.get("title", "") or ""
                content = h.get("content", "") or ""
                doc_text = f"{title}\n{content[:3000]}" if title else content[:3000]
                documents.append(doc_text)

            response = self._cohere_client.rerank(
                model="rerank-v3.5",
                query=query,
                documents=documents,
                top_n=min(top_k, len(hits)),
            )

            reranked = []
            for result in response.results:
                hit = hits[result.index]
                hit["rerank_score"] = result.relevance_score
                reranked.append(hit)

            # Apply ranking boost if this is a ranking query
            if ranking_intent and getattr(ranking_intent, "is_ranking", False) and school_data_map:
                reranked = self._apply_ranking_boost(
                    reranked, ranking_intent, school_data_map,
                )

            # Filter out low-relevance hits to avoid diluting context
            min_score = 0.1
            before_count = len(reranked)
            reranked = [h for h in reranked if h.get("rerank_score", 0) >= min_score]
            if len(reranked) < before_count:
                logger.info(
                    "Reranker filtered %d low-relevance hits (score < %.2f)",
                    before_count - len(reranked), min_score,
                )

            return reranked

        except Exception as exc:
            logger.warning("Cohere reranking failed, using retrieval order: %s", exc)
            return hits[:top_k]

    @staticmethod
    def _apply_ranking_boost(
        hits: List[Dict[str, Any]],
        ranking_intent: Any,
        school_data_map: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Boost rerank scores based on Niche rank and category grades.

        Modifies hits in-place and re-sorts by boosted score.
        """
        categories = getattr(ranking_intent, "categories", [])
        only_other = categories == ["other"]

        for hit in hits:
            college = (hit.get("college_name") or "").lower()
            sd = school_data_map.get(college)
            if sd is None:
                continue

            boost = 0.0
            original = hit.get("rerank_score", 0.0)

            # 1. Niche rank boost (skip for "other"-only)
            if not only_other:
                niche_rank = sd.get("niche_rank")
                if niche_rank and isinstance(niche_rank, (int, float)):
                    rank_score = max(0.0, 1.0 - (niche_rank - 1) / 500.0)
                    boost += rank_score * 0.15

            # 2. Acceptance rate boost (academics only)
            if "academics" in categories:
                ar = sd.get("acceptance_rate")
                if ar and isinstance(ar, (int, float)) and 0 < ar <= 1:
                    boost += (1.0 - ar) * 0.05

            # 3. Category grade boost
            if not only_other:
                grade_scores = []
                for cat in categories:
                    if cat == "other":
                        continue
                    grade_key = _CATEGORY_TO_GRADE_KEY.get(cat)
                    if not grade_key:
                        continue
                    grade_str = sd.get(grade_key)
                    if grade_str and grade_str in _GRADE_TO_NUM:
                        grade_scores.append(_GRADE_TO_NUM[grade_str] / 4.3)
                if grade_scores:
                    avg_grade = sum(grade_scores) / len(grade_scores)
                    boost += avg_grade * 0.10

            if boost > 0:
                hit["rerank_score"] = original + boost
                hit["ranking_boost"] = boost

        # Re-sort by boosted score
        hits.sort(key=lambda h: h.get("rerank_score", 0.0), reverse=True)
        boosted_count = sum(1 for h in hits if h.get("ranking_boost"))
        if boosted_count:
            logger.info(
                "Ranking boost applied to %d hits (categories=%s)",
                boosted_count, categories,
            )

        return hits
