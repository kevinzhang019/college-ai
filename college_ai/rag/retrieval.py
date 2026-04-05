"""
Hybrid Retrieval Engine for the College RAG system.

Uses Milvus 2.5 hybrid search: dense (COSINE) + sparse (BM25),
merged via Reciprocal Rank Fusion (RRF).

Pre-filters by college_name when specified, with fallback to
global search + soft boost if the school has sparse coverage.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from college_ai.rag.embeddings import get_embedding
from college_ai.scraping.config import (
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME_V2,
    VECTOR_DIM,
)

logger = logging.getLogger(__name__)

# Minimum results from a school-filtered search before we fall back to global
SCHOOL_FILTER_MIN_RESULTS = 4

# Max chunks from the same URL for source diversity
MAX_CHUNKS_PER_URL = int(os.getenv("RAG_MAX_CHUNKS_PER_URL", "2"))


class HybridRetriever:
    """Hybrid dense + BM25 retrieval over the colleges_v2 Milvus collection."""

    def __init__(
        self,
        collection_name: Optional[str] = None,
    ):
        self.collection_name = collection_name or ZILLIZ_COLLECTION_NAME_V2
        self._client = None

    # ---- Connection ----

    def _get_client(self):
        """Lazily initialize the MilvusClient."""
        if self._client is not None:
            return self._client

        from pymilvus import MilvusClient

        self._client = MilvusClient(uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
        logger.info(
            "Connected to Milvus (collection=%s)", self.collection_name
        )
        return self._client

    # ---- Core search ----

    def search(
        self,
        query: str,
        query_embedding: List[float],
        college_name: Optional[str] = None,
        top_k: int = 30,
    ) -> List[Dict[str, Any]]:
        """Run hybrid search (dense + BM25) with optional school pre-filter.

        Args:
            query: The search query text (used for BM25 arm).
            query_embedding: Pre-computed dense embedding vector.
            college_name: If set, pre-filter results to this school.
            top_k: Number of candidates to retrieve (before reranking).

        Returns:
            List of hit dicts with: college_name, url, title, content,
            crawled_at, distance, url_canonical.
        """
        if not query.strip():
            return []

        output_fields = [
            "college_name", "url", "url_canonical", "title",
            "content", "crawled_at",
        ]

        # Try school-specific search first
        if college_name:
            hits = self._hybrid_search(
                query, query_embedding, top_k,
                output_fields=output_fields,
                college_filter=college_name,
            )
            if len(hits) >= SCHOOL_FILTER_MIN_RESULTS:
                return self._dedupe_by_url(hits, top_k)

            # Sparse coverage — fall back to global + soft boost
            logger.info(
                "School filter returned %d results (< %d) for '%s', "
                "falling back to global search with boost.",
                len(hits), SCHOOL_FILTER_MIN_RESULTS, college_name,
            )
            global_hits = self._hybrid_search(
                query, query_embedding, top_k,
                output_fields=output_fields,
            )
            boosted = self._apply_school_boost(global_hits, college_name)
            return self._dedupe_by_url(boosted, top_k)

        # No school filter — global search
        hits = self._hybrid_search(
            query, query_embedding, top_k,
            output_fields=output_fields,
        )
        return self._dedupe_by_url(hits, top_k)

    def search_multi_query(
        self,
        queries: List[str],
        college_name: Optional[str] = None,
        top_k: int = 30,
    ) -> List[Dict[str, Any]]:
        """Run multiple queries and merge results (for essay mode).

        Embeds each query, runs hybrid search, deduplicates by URL.
        """
        all_hits = []
        seen_urls = set()

        for q in queries:
            if not q.strip():
                continue
            embedding = get_embedding(q)
            if embedding is None or len(embedding) != VECTOR_DIM:
                continue
            hits = self.search(q, embedding, college_name, top_k=top_k)
            for hit in hits:
                url = hit.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_hits.append(hit)

        return all_hits[:top_k]

    # ---- Internal ----

    def _hybrid_search(
        self,
        query: str,
        query_embedding: List[float],
        top_k: int,
        output_fields: List[str],
        college_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Execute hybrid dense + BM25 search on Milvus."""
        from pymilvus import AnnSearchRequest, RRFRanker

        client = self._get_client()

        # Build filter expression
        expr = None
        if college_filter:
            # Escape single quotes in college names
            safe_name = college_filter.replace("'", "\\'")
            expr = f"college_name == '{safe_name}'"

        # Dense retrieval arm (COSINE similarity)
        dense_req = AnnSearchRequest(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 32}},
            limit=top_k,
            expr=expr,
        )

        # Sparse/BM25 retrieval arm
        sparse_req = AnnSearchRequest(
            data=[query],  # raw text — Milvus tokenizes via BM25 function
            anns_field="content_sparse",
            param={"metric_type": "BM25"},
            limit=top_k,
            expr=expr,
        )

        try:
            results = client.hybrid_search(
                collection_name=self.collection_name,
                reqs=[dense_req, sparse_req],
                ranker=RRFRanker(k=60),
                limit=top_k,
                output_fields=output_fields,
            )
        except Exception as exc:
            logger.error("Hybrid search failed: %s", exc)
            # Fall back to dense-only search
            return self._dense_only_search(
                query_embedding, top_k, output_fields, expr
            )

        return self._normalize_results(results)

    def _dense_only_search(
        self,
        query_embedding: List[float],
        top_k: int,
        output_fields: List[str],
        expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fallback: dense-only search if hybrid fails."""
        client = self._get_client()
        try:
            results = client.search(
                collection_name=self.collection_name,
                data=[query_embedding],
                anns_field="embedding",
                search_params={"metric_type": "COSINE", "params": {"nprobe": 32}},
                limit=top_k,
                filter=expr,
                output_fields=output_fields,
            )
            return self._normalize_results(results)
        except Exception as exc:
            logger.error("Dense-only search also failed: %s", exc)
            return []

    @staticmethod
    def _normalize_results(results) -> List[Dict[str, Any]]:
        """Convert Milvus search results to flat dicts."""
        if not results:
            return []

        hits = []
        # MilvusClient returns List[List[dict]]
        result_list = results if isinstance(results, list) else [results]
        for group in result_list:
            if not group:
                continue
            # Each group may be a list of hits or a single result set
            items = group if isinstance(group, list) else [group]
            for item in items:
                if isinstance(item, dict):
                    record = dict(item)
                    # Normalize the distance/score field
                    if "distance" not in record:
                        record["distance"] = record.get("score", 0.0)
                    hits.append(record)
                else:
                    # ORM-style hit objects
                    try:
                        entity = getattr(item, "entity", item)
                        record = {}
                        for key in [
                            "college_name", "url", "url_canonical",
                            "title", "content", "crawled_at",
                        ]:
                            record[key] = (
                                entity.get(key) if hasattr(entity, "get")
                                else getattr(entity, key, None)
                            )
                        record["distance"] = getattr(
                            item, "distance", getattr(item, "score", 0.0)
                        )
                        hits.append(record)
                    except Exception:
                        continue
        return hits

    @staticmethod
    def _apply_school_boost(
        hits: List[Dict[str, Any]],
        target_college: str,
        boost_factor: float = 0.15,
    ) -> List[Dict[str, Any]]:
        """Boost results from the target college without hard-filtering others."""
        college_lc = target_college.lower()
        for rec in hits:
            name = str(rec.get("college_name", "")).lower()
            dist = float(rec.get("distance", 0.0) or 0.0)
            if name == college_lc:
                rec["_boosted_distance"] = dist - boost_factor
            else:
                rec["_boosted_distance"] = dist
        hits.sort(key=lambda r: r["_boosted_distance"])
        return hits

    @staticmethod
    def _dedupe_by_url(
        hits: List[Dict[str, Any]], top_k: int
    ) -> List[Dict[str, Any]]:
        """Limit chunks per URL for source diversity, then truncate."""
        url_counts: Dict[str, int] = {}
        deduped: List[Dict[str, Any]] = []
        for rec in hits:
            url = rec.get("url") or ""
            count = url_counts.get(url, 0)
            if count < MAX_CHUNKS_PER_URL:
                url_counts[url] = count + 1
                deduped.append(rec)
            if len(deduped) >= top_k:
                break
        return deduped
