"""
College RAG Service

Provides retrieval-augmented generation over the Zilliz/Milvus collection
populated by the multithreaded crawler. Designed to help students apply to
college by answering questions using crawled college site content.

Key features:
- Connects to Zilliz/Milvus using settings from `college_ai.scraping.config`
- Embeds queries with OpenAI (reuses utilities in `utils/openai_embed.py`)
- Vector search over the `embedding` field with optional filters
- Answer generation via OpenAI Chat with citations

Usage (CLI):
    python -m college_ai.rag.service --question "How do I apply for CS at MIT?" --top_k 8

Programmatic usage:
    from college_ai.rag.service import CollegeRAG
    rag = CollegeRAG()
    result = rag.answer_question("Best scholarships for business majors?")
    print(result["answer"])  # formatted text with citations
    print(result["sources"])  # list of sources
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from pymilvus import Collection, connections, utility

from college_ai.scraping.config import (
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME,
    VECTOR_DIM,
    METRIC_TYPE,
)
from college_ai.rag.embeddings import get_embedding


# Ensure project .env is loaded (same pattern as other utils)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH)

# Maximum L2 distance for a hit to be considered relevant.
# Hits beyond this threshold are dropped to avoid injecting noise into the LLM context.
MAX_L2_DISTANCE = float(os.getenv("RAG_MAX_L2_DISTANCE", "1.2"))

# Maximum number of chunks from the same URL to include in results
MAX_CHUNKS_PER_URL = int(os.getenv("RAG_MAX_CHUNKS_PER_URL", "2"))

# Canned response when no relevant sources are found
NO_ANSWER_RESPONSE = (
    "I don't have specific information about that in my sources. "
    "Please check the college's official website directly for the most "
    "accurate and up-to-date information."
)


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


# Common academic major/field keywords used for content-based boosting.
# When a user query mentions one of these, chunks containing the term are ranked higher.
_MAJOR_KEYWORDS = [
    "computer science", "software engineering", "computer engineering",
    "information technology", "data science", "cybersecurity",
    "business", "finance", "accounting", "marketing", "economics",
    "management", "entrepreneurship",
    "engineering", "mechanical engineering", "electrical engineering",
    "civil engineering", "chemical engineering", "biomedical engineering",
    "aerospace engineering",
    "biology", "chemistry", "physics", "mathematics", "statistics",
    "environmental science",
    "nursing", "medicine", "pharmacy", "public health",
    "psychology", "sociology", "political science", "anthropology",
    "criminal justice", "social work", "international relations",
    "english", "literature", "history", "philosophy",
    "art", "music", "theater", "communications",
    "education",
]


def _extract_major_keywords(question: str) -> List[str]:
    """Return major keywords found in the user's question (longest-match first)."""
    q_lower = question.lower()
    found = [kw for kw in _MAJOR_KEYWORDS if kw in q_lower]
    # Sort longest first so multi-word majors take priority
    found.sort(key=len, reverse=True)
    return found


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
            "OPENAI_CHAT_MODEL", "gpt-4.1-nano"
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

    # ---- Query rewriting ----
    def _rewrite_query(self, question: str) -> str:
        """Expand a short/ambiguous user query into a search-optimized version.

        Uses a lightweight LLM call. Falls back to the original query on failure.
        """
        # Skip rewriting for already-detailed queries (>60 chars)
        if len(question.strip()) > 60:
            return question

        try:
            client = self._get_chat_client()
            response = client.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Rewrite this college admissions question as a detailed search query "
                            "optimized for semantic search over college website content. "
                            "Keep all specific details (school names, scores, dates). "
                            "Output only the rewritten query, nothing else."
                        ),
                    },
                    {"role": "user", "content": question},
                ],
                temperature=0,
                max_tokens=150,
            )
            if response and response.choices:
                rewritten = response.choices[0].message.content or ""
                return rewritten.strip() or question
        except Exception:
            pass
        return question

    # ---- Retrieval ----
    def search(
        self,
        question: str,
        top_k: int = 8,
        *,
        college_name: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search Milvus for relevant chunks.

        Args:
            question: user query text
            top_k: number of hits to return
            college_name: optional college name filter (HARD FILTER - only returns results from this college)
            output_fields: fields to return; defaults sensible

        Returns:
            List of hit dicts with keys: url, title, content, college_name, crawled_at, distance
        """
        if not question or not question.strip():
            return []

        # Rewrite short/ambiguous queries for better retrieval
        search_query = self._rewrite_query(question)

        # Embed query
        embedding = get_embedding(search_query)
        if embedding is None or len(embedding) != VECTOR_DIM:
            return []

        # Build optional filter expression (only scalar fields; JSON filtering done post-search)
        # Note: We'll do flexible college filtering post-search for better user experience
        expr = None

        # Default output fields
        ofields = output_fields or [
            "college_name",
            "url",
            "title",
            "content",
            "crawled_at",
        ]

        # Milvus search - get more results if we need to filter
        search_limit = top_k * 3 if college_name else top_k
        try:
            results = self.collection.search(
                data=[embedding],
                anns_field="embedding",
                param={"metric_type": METRIC_TYPE, "params": {"nprobe": self.nprobe}},
                limit=search_limit,
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

        # Apply college filtering and keyword-based reranking
        if college_name:
            college_lc = college_name.strip().lower()
            normalized = [
                rec for rec in normalized
                if str(rec.get("college_name", "")).lower().strip() == college_lc
            ]

        # Boost chunks whose content mentions major keywords from the query
        major_keywords = _extract_major_keywords(question)
        if major_keywords:
            ranked = []
            for rec in normalized:
                content_lc = str(rec.get("content", "")).lower()
                title_lc = str(rec.get("title", "")).lower()
                text = content_lc + " " + title_lc
                hits_count = sum(1 for kw in major_keywords if kw in text)
                dist = rec.get("distance", 0.0) or 0.0
                boost = 0.05 * hits_count
                ranked.append((dist - boost, rec))
            ranked.sort(key=lambda x: x[0])
            normalized = [rec for _, rec in ranked][:top_k]
        else:
            normalized = normalized[:top_k]

        # Drop hits beyond the relevance distance threshold
        normalized = [
            rec for rec in normalized
            if (rec.get("distance") or 0.0) <= MAX_L2_DISTANCE
        ]

        # Limit chunks per URL for source diversity, then deduplicate
        url_counts: Dict[str, int] = {}
        deduped: List[Dict[str, Any]] = []
        for rec in normalized:
            url = rec.get("url") or ""
            count = url_counts.get(url, 0)
            if count < MAX_CHUNKS_PER_URL:
                url_counts[url] = count + 1
                deduped.append(rec)

        return deduped

    # ---- Generation ----
    def _get_chat_client(self):
        if self._chat_client is None:
            self._chat_client = _get_openai_chat_client()
        return self._chat_client

    @staticmethod
    def _build_context_snippets(hits: List[Dict[str, Any]]) -> List[str]:
        """Format top retrievals as numbered snippets for the prompt.

        Dynamically scales snippet length: fewer hits → more context per hit.
        Includes crawled_at so the LLM can flag stale information.
        """
        # Scale snippet length inversely with hit count
        if len(hits) <= 3:
            max_chars = 2500
        elif len(hits) <= 6:
            max_chars = 1800
        else:
            max_chars = 1500

        snippets: List[str] = []
        for idx, rec in enumerate(hits, start=1):
            college = rec.get("college_name") or ""
            title = rec.get("title") or ""
            url = rec.get("url") or ""
            crawled_at = rec.get("crawled_at") or "unknown"
            content = rec.get("content") or ""
            content = content.strip()
            if len(content) > max_chars:
                content = content[:max_chars] + "..."
            snippet = f"[{idx}] {college} — {title} (crawled: {crawled_at})\nURL: {url}\n{content}"
            snippets.append(snippet)
        return snippets

    @staticmethod
    def _verify_citations(answer: str, num_sources: int) -> str:
        """Strip or fix citation references that exceed the number of provided sources.

        Also appends a warning if the answer contains no citations at all despite
        having sources available.
        """
        if num_sources == 0:
            return answer

        def _replace_invalid(match: re.Match) -> str:
            cited = int(match.group(1))
            if cited < 1 or cited > num_sources:
                return ""  # Strip invalid citation
            return match.group(0)

        cleaned = re.sub(r"\[(\d+)\]", _replace_invalid, answer)

        # Check that at least one valid citation remains
        valid_citations = re.findall(r"\[\d+\]", cleaned)
        if not valid_citations and num_sources > 0:
            cleaned += (
                "\n\n> **Note:** This response may not be fully grounded in the "
                "provided sources. Please verify the information with the college's "
                "official website."
            )

        return cleaned

    def _rerank(
        self,
        question: str,
        hits: List[Dict[str, Any]],
        top_k: int = 8,
    ) -> List[Dict[str, Any]]:
        """Rerank retrieved hits by asking the LLM to score relevance.

        Falls back to the original order on failure.
        """
        if len(hits) <= 1:
            return hits

        try:
            client = self._get_chat_client()
            # Build compact descriptions for scoring
            descriptions = []
            for i, rec in enumerate(hits):
                title = rec.get("title") or ""
                content = (rec.get("content") or "")[:300]
                descriptions.append(f"[{i}] {title}: {content}")

            prompt = (
                f"Question: {question}\n\n"
                "Rate each source's relevance to the question on a scale of 0-10. "
                "Return ONLY a JSON array of scores in the same order, e.g. [8, 3, 7, ...]\n\n"
                + "\n".join(descriptions)
            )

            response = client.chat.completions.create(
                model=self.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=100,
            )
            if response and response.choices:
                import json
                text = response.choices[0].message.content or ""
                # Extract JSON array from response
                match = re.search(r"\[[\d\s,\.]+\]", text)
                if match:
                    scores = json.loads(match.group())
                    if len(scores) == len(hits):
                        paired = list(zip(scores, hits))
                        paired.sort(key=lambda x: x[0], reverse=True)
                        return [h for _, h in paired][:top_k]
        except Exception:
            pass
        return hits[:top_k]

    def _generate_answer(
        self,
        question: str,
        contexts: List[Dict[str, Any]],
        *,
        college_name: Optional[str] = None,
    ) -> str:
        """Call OpenAI chat to compose an answer using the retrieved contexts."""
        client = self._get_chat_client()
        snippets = self._build_context_snippets(contexts)
        sources_block = "\n\n".join(snippets) if snippets else ""

        system_preamble = (
            "You are a helpful college application assistant specializing in UNDERGRADUATE admissions."
            " Use only the provided sources to answer."
            " Always cite as [1], [2], ... mapping to the numbered sources."
            "\n\nGROUNDEDNESS RULES (strictly follow these):"
            "\n- Every factual claim MUST have a citation [N]. If you cannot cite it from the sources, do not state it."
            "\n- If the sources do not contain enough information to fully answer the question, say so explicitly. Do not guess or fill in gaps with general knowledge."
            "\n- Never invent or fabricate URLs, deadlines, dollar amounts, acceptance rates, or statistics that are not explicitly present in the sources."
            "\n- If only partial information is available, answer what you can from the sources and clearly state what is missing."
            "\n\nFORMATTING:"
            "\n- Use ## for main headings, ### for subheadings"
            "\n- Use **bold** for emphasis on important points"
            "\n- Use - for bullet points in lists"
            "\n- Use proper line breaks and spacing"
            "\n\nFocus exclusively on undergraduate (bachelor's degree) programs, requirements, and admissions."
            " If sources mention graduate programs (Master's/PhD), adapt the information for undergraduate context or note it's not applicable."
        )
        if college_name:
            system_preamble += f"\nPrioritize information for {college_name} if present."

        # Inject ML prediction context if applicable
        prediction_context = ""
        try:
            from college_ai.rag.bridge import get_prediction_context
            ctx = get_prediction_context(question, college_name=college_name)
            if ctx:
                prediction_context = f"\n{ctx}\n"
        except Exception:
            pass

        # Detect if the question is action-oriented (application steps, how-to)
        q_lower = question.lower()
        is_actionable = any(
            kw in q_lower
            for kw in ["how do i", "how to", "apply", "application", "steps", "deadline", "require", "submit"]
        )

        instructions = [
            "- Focus on undergraduate programs and admissions. Only mention graduate programs if explicitly asked.",
            "- Include admission requirements, deadlines, and scholarships when the sources contain this information.",
            "- Only state facts that appear in the sources. Cite every claim.",
            "- If ML model prediction data is provided above, incorporate it naturally into your answer with appropriate caveats.",
        ]
        if is_actionable:
            instructions.append("- End with a ## Next Steps section using bullet points for undergraduate applicants.")

        user_prompt = (
            f"Question: {question}\n\n"
            f"Sources:\n{sources_block}\n\n"
            f"{prediction_context}"
            "Instructions:\n" + "\n".join(instructions) + "\n"
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
        college_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run end-to-end RAG: retrieve then generate an answer.

        Returns a dict with keys: answer (str), sources (List[Dict]).
        """
        hits = self.search(
            question,
            top_k=top_k,
            college_name=college_name,
        )
        if not hits:
            return {"answer": NO_ANSWER_RESPONSE, "sources": []}
        hits = self._rerank(question, hits, top_k=top_k)
        answer = self._generate_answer(
            question, hits, college_name=college_name
        )
        answer = self._verify_citations(answer, len(hits))

        # Compute confidence metadata from source distances
        distances = [h.get("distance", 0.0) or 0.0 for h in hits]
        avg_distance = sum(distances) / len(distances) if distances else 0.0
        if len(hits) >= 4 and avg_distance < 0.6:
            confidence = "high"
        elif len(hits) >= 2 and avg_distance < 0.9:
            confidence = "medium"
        else:
            confidence = "low"

        return {
            "answer": answer,
            "sources": hits,
            "confidence": confidence,
            "source_count": len(hits),
            "avg_distance": round(avg_distance, 3),
        }


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
    parser.add_argument(
        "--college", type=str, default=None, help="Optional exact college name filter"
    )
    args = parser.parse_args(argv)

    rag = CollegeRAG()
    result = rag.answer_question(
        args.question, top_k=args.top_k, college_name=args.college
    )
    print("\n==== Answer ====")
    print(result.get("answer", ""))
    print("\n==== Sources ====")
    print(_format_sources_for_cli(result.get("sources", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main_cli())
