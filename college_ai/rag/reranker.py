"""
Cross-encoder reranking for the College RAG system.

Primary: Cohere rerank-v3.5 (requires COHERE_API_KEY env var).
Fallback: passthrough (returns candidates in their original order).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


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
    ) -> List[Dict[str, Any]]:
        """Rerank hits by relevance to the query.

        Args:
            query: The user's question text.
            hits: Candidate documents from hybrid retrieval.
            top_k: Number of top results to return.

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
