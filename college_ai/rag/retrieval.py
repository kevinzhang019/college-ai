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
import threading
from typing import Any, Dict, List, Optional

from college_ai.rag.embeddings import get_embedding
from college_ai.scraping.config import (
    RAG_DENSE_WEIGHT,
    RAG_RANKER_RRF_K,
    RAG_RANKER_TYPE,
    RAG_SCHOOL_BOOST,
    RAG_SPARSE_WEIGHT,
    RETRIEVAL_NPROBE,
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME,
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
        self.collection_name = collection_name or ZILLIZ_COLLECTION_NAME
        self._client = None
        self._client_lock = threading.Lock()

    # ---- Connection ----

    def _get_collection(self):
        """Lazily initialize the ORM Collection handle (thread-safe).

        Uses double-checked locking so concurrent FastAPI requests don't
        race on connection setup. Uses the ORM API instead of MilvusClient
        to avoid connection hangs on Zilliz Serverless.
        """
        if self._client is not None:
            return self._client

        with self._client_lock:
            if self._client is not None:  # double-check after acquiring lock
                return self._client

            from pymilvus import connections, Collection

            connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
            client = Collection(self.collection_name)
            client.load()
            self._client = client
            logger.info(
                "Connected to Milvus (collection=%s)", self.collection_name
            )
        return self._client

    # ---- Core search ----

    def search(
        self,
        query: str,
        query_embedding: List[float],
        college_names: Optional[List[str]] = None,
        top_k: int = 30,
    ) -> List[Dict[str, Any]]:
        """Run hybrid search (dense + BM25) with optional school pre-filter.

        Args:
            query: The search query text (used for BM25 arm).
            query_embedding: Pre-computed dense embedding vector.
            college_names: If set, pre-filter results to these schools.
            top_k: Number of candidates to retrieve (before reranking).

        Returns:
            List of hit dicts with: college_name, url, title, content,
            page_type, crawled_at, distance, url_canonical.
        """
        if not query.strip():
            return []

        output_fields = [
            "college_name", "url", "url_canonical", "title",
            "content", "page_type", "crawled_at",
        ]

        # Try school-specific search first
        if college_names:
            hits = self._hybrid_search(
                query, query_embedding, top_k,
                output_fields=output_fields,
                college_filter=college_names,
            )
            min_results = SCHOOL_FILTER_MIN_RESULTS * len(college_names)
            if len(hits) >= min_results:
                result = self._dedupe_by_url(hits, top_k)
                logger.info(
                    "Search(schools=%s): %d raw → %d deduped",
                    college_names, len(hits), len(result),
                )
                return result

            # Sparse coverage — fall back to global + soft boost
            logger.info(
                "School filter returned %d results (< %d) for %s, "
                "falling back to global search with boost.",
                len(hits), min_results, college_names,
            )
            global_hits = self._hybrid_search(
                query, query_embedding, top_k,
                output_fields=output_fields,
            )
            boosted = self._apply_school_boost(global_hits, college_names)
            result = self._dedupe_by_url(boosted, top_k)
            logger.info(
                "Search(global+boost for %s): %d raw → %d deduped",
                college_names, len(global_hits), len(result),
            )
            return result

        # No school filter — global search
        hits = self._hybrid_search(
            query, query_embedding, top_k,
            output_fields=output_fields,
        )
        result = self._dedupe_by_url(hits, top_k)
        logger.info(
            "Search(global): %d raw → %d deduped",
            len(hits), len(result),
        )
        return result

    def search_multi_query(
        self,
        queries: List[str],
        college_names: Optional[List[str]] = None,
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
            hits = self.search(q, embedding, college_names, top_k=top_k)
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
        college_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Execute hybrid dense + BM25 search on Milvus."""
        from pymilvus import AnnSearchRequest, RRFRanker, WeightedRanker

        col = self._get_collection()

        # Build filter expression
        expr = None
        if college_filter:
            if len(college_filter) == 1:
                safe_name = college_filter[0].replace("'", "\\'")
                expr = f"college_name == '{safe_name}'"
            else:
                safe_names = [n.replace("'", "\\'") for n in college_filter]
                names_str = "', '".join(safe_names)
                expr = f"college_name in ['{names_str}']"

        # Dense retrieval arm (COSINE similarity)
        dense_req = AnnSearchRequest(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": RETRIEVAL_NPROBE}},
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

        # Select ranker: RRF (default) or WeightedRanker (configurable)
        if RAG_RANKER_TYPE == "weighted":
            ranker = WeightedRanker(RAG_DENSE_WEIGHT, RAG_SPARSE_WEIGHT)
        else:
            ranker = RRFRanker(k=RAG_RANKER_RRF_K)

        try:
            results = col.hybrid_search(
                reqs=[dense_req, sparse_req],
                rerank=ranker,
                limit=top_k,
                output_fields=output_fields,
            )
        except Exception as exc:
            logger.error("Hybrid search failed: %s", exc)
            # Fall back to dense-only search
            return self._dense_only_search(
                query_embedding, top_k, output_fields, expr
            )

        hits = self._normalize_results(results)
        if hits:
            sample_dists = [h.get("distance", 0) for h in hits[:3]]
            logger.debug(
                "Hybrid search sample distances (first 3): %s "
                "(convention: RRF score, higher = more relevant)",
                sample_dists,
            )
        return hits

    def _dense_only_search(
        self,
        query_embedding: List[float],
        top_k: int,
        output_fields: List[str],
        expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fallback: dense-only search if hybrid fails."""
        col = self._get_collection()
        try:
            results = col.search(
                data=[query_embedding],
                anns_field="embedding",
                param={"metric_type": "COSINE", "params": {"nprobe": RETRIEVAL_NPROBE}},
                limit=top_k,
                expr=expr,
                output_fields=output_fields,
            )
            return self._normalize_results(results)
        except Exception as exc:
            logger.error("Dense-only search also failed: %s", exc)
            return []

    _OUTPUT_FIELDS = [
        "college_name", "url", "url_canonical",
        "title", "content", "page_type", "crawled_at",
    ]

    @classmethod
    def _normalize_results(cls, results) -> List[Dict[str, Any]]:
        """Convert Milvus search results to flat dicts.

        Handles all pymilvus result variants (dict, Hit, SearchResult) and
        always flattens the ``entity`` sub-object so output fields live at
        the top level.
        """
        if not results:
            return []

        hits = []
        result_list = results if isinstance(results, list) else [results]
        for group in result_list:
            if not group:
                continue
            items = group if isinstance(group, list) else [group]
            for item in items:
                try:
                    record = cls._flatten_hit(item)
                    if record:
                        hits.append(record)
                except Exception:
                    continue

        if hits:
            sample = hits[0]
            logger.debug(
                "Normalized %d results. Sample: college=%s url=%s title=%s content_len=%d",
                len(hits),
                sample.get("college_name"),
                (sample.get("url") or "")[:80],
                (sample.get("title") or "")[:50],
                len(sample.get("content") or ""),
            )
        return hits

    @classmethod
    def _flatten_hit(cls, item) -> Optional[Dict[str, Any]]:
        """Extract a flat dict from any pymilvus Hit variant.

        Pymilvus Hit objects vary across versions: some inherit from dict,
        some use an ``entity`` attribute, some use ``.get()``. This method
        tries all access patterns and always returns a flat dict with the
        output fields at the top level.
        """
        # --- 1. Try to get the entity (where output fields usually live) ---
        entity = None  # type: Any
        if hasattr(item, "entity"):
            entity = item.entity
        elif isinstance(item, dict) and "entity" in item:
            entity = item["entity"]

        # --- 2. Build a helper that reads a key from entity-then-item ---
        def _get(key: str):
            # Try entity first (preferred), then the item itself
            for src in (entity, item):
                if src is None:
                    continue
                if hasattr(src, "get"):
                    val = src.get(key)
                    if val is not None:
                        return val
                val = getattr(src, key, None)
                if val is not None:
                    return val
            return None

        record = {}  # type: Dict[str, Any]
        for key in cls._OUTPUT_FIELDS:
            record[key] = _get(key)

        # Distance / score
        dist = _get("distance")
        if dist is None:
            dist = _get("score")
        record["distance"] = float(dist) if dist is not None else 0.0

        # Preserve id if present (for debugging)
        hit_id = _get("id")
        if hit_id is not None:
            record["id"] = hit_id

        return record

    @staticmethod
    def _apply_school_boost(
        hits: List[Dict[str, Any]],
        target_colleges: List[str],
        boost_factor: float = RAG_SCHOOL_BOOST,
    ) -> List[Dict[str, Any]]:
        """Boost results from the target colleges without hard-filtering others.

        RRF/hybrid search returns scores where higher = more relevant.
        Adding boost_factor increases the target schools' effective score.
        Sort descending so highest-scoring results come first.
        """
        targets_lc = {c.lower() for c in target_colleges}
        for rec in hits:
            name = str(rec.get("college_name", "")).lower()
            score = float(rec.get("distance", 0.0) or 0.0)
            if name in targets_lc:
                rec["_boosted_score"] = score + boost_factor
            else:
                rec["_boosted_score"] = score
        hits.sort(key=lambda r: r["_boosted_score"], reverse=True)
        return hits

    @staticmethod
    def _dedupe_by_url(
        hits: List[Dict[str, Any]], top_k: int
    ) -> List[Dict[str, Any]]:
        """Limit chunks per URL for source diversity, then truncate.

        Records with no URL are always kept (never deduped against each other).
        """
        url_counts: Dict[str, int] = {}
        deduped: List[Dict[str, Any]] = []
        for rec in hits:
            url = rec.get("url") or ""
            if not url:
                deduped.append(rec)
            else:
                count = url_counts.get(url, 0)
                if count < MAX_CHUNKS_PER_URL:
                    url_counts[url] = count + 1
                    deduped.append(rec)
            if len(deduped) >= top_k:
                break
        return deduped
