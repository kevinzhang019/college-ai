"""
College RAG Service

Provides retrieval-augmented generation over the Zilliz/Milvus collection
populated by the multithreaded crawler. Designed to help students apply to
college by answering questions using crawled college site content.

Key features:
- Connects to Zilliz/Milvus using settings from `preference_scraper.crawlers.config`
- Embeds queries with OpenAI (reuses utilities in `utils/openai_embed.py`)
- Vector search over the `embedding` field with optional filters
- Optional major-aware reranking using the `majors` JSON field
- Answer generation via OpenAI Chat with citations

Usage (CLI):
    python -m preference_scraper.utils.rag_service --question "How do I apply for CS at MIT?" --top_k 8

Programmatic usage:
    from preference_scraper.utils.rag_service import CollegeRAG
    rag = CollegeRAG()
    result = rag.answer_question("Best scholarships for business majors?", major="business")
    print(result["answer"])  # formatted text with citations
    print(result["sources"])  # list of sources
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pymilvus import Collection, connections, utility

from preference_scraper.crawlers.config import (
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME,
    VECTOR_DIM,
    METRIC_TYPE,
)
from preference_scraper.utils.openai_embed import get_embedding


# Ensure project .env is loaded (same pattern as other utils)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH)


def _get_openai_chat_client():
    """Create an OpenAI client for chat completions using env var OPENAI_API_KEY.

    We use the same import path as embedding utils (openai>=1.14.x).
    """
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover - import failure
        raise RuntimeError(
            "openai package is required for generation. Please install it."
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY", "").strip().strip('"').strip("'")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not found in environment.")
    return OpenAI(api_key=api_key)


def _extract_hit_fields(hit: Any, field_names: List[str]) -> Dict[str, Any]:
    """Robustly extract requested fields from a Milvus search hit.

    PyMilvus versions expose fields via different attributes (entity/fields).
    This helper normalizes access.
    """
    record: Dict[str, Any] = {}
    # Newer PyMilvus exposes "entity" with dict-like access
    entity = getattr(hit, "entity", None)
    if entity is not None and hasattr(entity, "get"):
        for fname in field_names:
            try:
                record[fname] = entity.get(fname)
            except Exception:
                record[fname] = None
        return record

    # Fallback: some versions use dict-like hit
    try:
        for fname in field_names:
            record[fname] = getattr(hit, fname, None)
    except Exception:
        pass
    return record


class CollegeRAG:
    """RAG engine over the `college_pages` Milvus collection.

    Methods:
        - search: retrieve top-k chunks with optional filters and reranking
        - answer_question: generate an answer from retrieved contexts
    """

    def __init__(
        self,
        collection_name: Optional[str] = None,
        openai_model: Optional[str] = None,
        nprobe: int = 16,
    ) -> None:
        self.collection_name = collection_name or ZILLIZ_COLLECTION_NAME
        self.openai_model = openai_model or os.getenv(
            "OPENAI_CHAT_MODEL", "gpt-4o-mini"
        )
        self.nprobe = int(os.getenv("MILVUS_NPROBE", str(nprobe)) or nprobe)

        # Connect and get collection
        self._connect_milvus()
        self.collection = self._get_collection()
        if self.collection is None:
            raise RuntimeError(
                f"Collection '{self.collection_name}' not found. Create it via crawler or recreate utility."
            )
        # Try to load if not already
        try:
            self.collection.load(timeout=60)
        except Exception:
            pass

        # Lazy chat client
        self._chat_client = None

    # ---- Milvus connection ----
    def _connect_milvus(self) -> None:
        try:
            connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
        except Exception as exc:
            raise RuntimeError(f"Failed to connect to Zilliz/Milvus: {exc}") from exc

    def _get_collection(self) -> Optional[Collection]:
        try:
            if utility.has_collection(self.collection_name):
                return Collection(self.collection_name)
            return None
        except Exception as exc:
            raise RuntimeError(f"Error accessing collection: {exc}") from exc

    # ---- Retrieval ----
    def search(
        self,
        question: str,
        top_k: int = 8,
        *,
        college_name: Optional[str] = None,
        major: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search Milvus for relevant chunks.

        Args:
            question: user query text
            top_k: number of hits to return
            college_name: optional exact college name filter
            major: optional major hint; used in prompt and minor reranking
            output_fields: fields to return; defaults sensible

        Returns:
            List of hit dicts with keys: url, title, content, college_name, majors, crawled_at, distance
        """
        if not question or not question.strip():
            return []

        # Embed query
        embedding = get_embedding(question)
        if embedding is None or len(embedding) != VECTOR_DIM:
            return []

        # Build optional filter expression (only scalar fields; JSON filtering done post-search)
        expr = None
        if college_name:
            safe = college_name.replace('"', '\\"')
            expr = f'college_name == "{safe}"'

        # Default output fields
        ofields = output_fields or [
            "college_name",
            "url",
            "title",
            "content",
            "majors",
            "crawled_at",
        ]

        # Milvus search
        try:
            results = self.collection.search(
                data=[embedding],
                anns_field="embedding",
                param={"metric_type": METRIC_TYPE, "params": {"nprobe": self.nprobe}},
                limit=top_k * 2 if major else top_k,
                expr=expr,
                output_fields=ofields,
            )
        except Exception as exc:
            raise RuntimeError(f"Milvus search failed: {exc}") from exc

        if not results:
            return []

        hits = results[0]
        normalized: List[Dict[str, Any]] = []
        for hit in hits:
            distance = getattr(hit, "distance", getattr(hit, "score", None))
            record = _extract_hit_fields(hit, ofields)
            record["distance"] = float(distance) if distance is not None else None
            normalized.append(record)

        # Optional major-aware reranking and filtering
        if major:
            major_lc = major.strip().lower()

            def has_major(rec: Dict[str, Any]) -> bool:
                val = rec.get("majors")
                if isinstance(val, list):
                    return any(str(m).strip().lower() == major_lc for m in val)
                if isinstance(val, dict) and "list" in val:
                    return any(
                        str(m).strip().lower() == major_lc for m in val.get("list", [])
                    )
                return False

            reranked: List[Tuple[float, Dict[str, Any]]] = []
            for rec in normalized:
                dist = rec.get("distance", 0.0) or 0.0
                # For L2 distance: smaller is better. Boost major matches by subtracting a tiny margin.
                adj = dist - 0.05 if has_major(rec) else dist
                reranked.append((adj, rec))
            reranked.sort(key=lambda x: x[0])
            normalized = [rec for _, rec in reranked][:top_k]
        else:
            # Just take top_k by original order
            normalized = normalized[:top_k]

        # Deduplicate by URL while preserving order
        seen_urls = set()
        deduped: List[Dict[str, Any]] = []
        for rec in normalized:
            url = rec.get("url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                deduped.append(rec)

        return deduped

    # ---- Generation ----
    def _get_chat_client(self):
        if self._chat_client is None:
            self._chat_client = _get_openai_chat_client()
        return self._chat_client

    @staticmethod
    def _build_context_snippets(hits: List[Dict[str, Any]]) -> List[str]:
        """Format top retrievals as numbered snippets for the prompt."""
        snippets: List[str] = []
        for idx, rec in enumerate(hits, start=1):
            college = rec.get("college_name") or ""
            title = rec.get("title") or ""
            url = rec.get("url") or ""
            content = rec.get("content") or ""
            # Keep snippets compact
            content = content.strip()
            if len(content) > 800:
                content = content[:800] + "..."
            snippet = f"[{idx}] {college} — {title}\nURL: {url}\n{content}"
            snippets.append(snippet)
        return snippets

    def _generate_answer(
        self,
        question: str,
        contexts: List[Dict[str, Any]],
        *,
        major: Optional[str] = None,
        college_name: Optional[str] = None,
    ) -> str:
        """Call OpenAI chat to compose an answer using the retrieved contexts."""
        client = self._get_chat_client()
        snippets = self._build_context_snippets(contexts)
        sources_block = "\n\n".join(snippets) if snippets else ""

        system_preamble = (
            "You are a helpful college application assistant."
            " Use only the provided sources to answer."
            " Always cite as [1], [2], ... mapping to the numbered sources."
            " Provide concise, practical guidance with bullet points."
        )
        if major:
            system_preamble += f" Focus on the '{major}' major when relevant."
        if college_name:
            system_preamble += f" Prioritize information for {college_name} if present."

        user_prompt = (
            f"Question: {question}\n\n"
            f"Sources:\n{sources_block}\n\n"
            "Instructions:\n"
            "- If insufficient evidence, say what is unknown.\n"
            "- Include application steps, requirements, deadlines, and scholarships when applicable.\n"
            "- End with a short 'Next steps' checklist.\n"
        )

        response = client.chat.completions.create(
            model=self.openai_model,
            messages=[
                {"role": "system", "content": system_preamble},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        if response and response.choices:
            return response.choices[0].message.content or ""
        return ""

    # ---- High-level API ----
    def answer_question(
        self,
        question: str,
        *,
        top_k: int = 8,
        major: Optional[str] = None,
        college_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run end-to-end RAG: retrieve then generate an answer.

        Returns a dict with keys: answer (str), sources (List[Dict]).
        """
        hits = self.search(
            question,
            top_k=top_k,
            college_name=college_name,
            major=major,
        )
        answer = self._generate_answer(
            question, hits, major=major, college_name=college_name
        )
        return {"answer": answer, "sources": hits}


def _format_sources_for_cli(sources: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, s in enumerate(sources, start=1):
        college = s.get("college_name", "")
        title = s.get("title", "")
        url = s.get("url", "")
        lines.append(f"[{idx}] {college} — {title}\n    {url}")
    return "\n".join(lines)


def _main_cli(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="College RAG CLI")
    parser.add_argument("--question", required=True, help="User question")
    parser.add_argument("--top_k", type=int, default=8, help="Top-k hits")
    parser.add_argument("--major", type=str, default=None, help="Optional major filter")
    parser.add_argument(
        "--college", type=str, default=None, help="Optional exact college name filter"
    )
    args = parser.parse_args(argv)

    rag = CollegeRAG()
    result = rag.answer_question(
        args.question, top_k=args.top_k, major=args.major, college_name=args.college
    )
    print("\n==== Answer ====")
    print(result.get("answer", ""))
    print("\n==== Sources ====")
    print(_format_sources_for_cli(result.get("sources", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main_cli())
