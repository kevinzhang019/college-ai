"""
College RAG Service — v2

Orchestrates: Router → Hybrid Retrieval → Reranker → Generator (QA or Essay).

Two core components:
  1. University Q&A — grounded answers about specific colleges
  2. Essay Helper — brainstorm ideas or review a draft with school-specific context

Usage (CLI):
    python -m college_ai.rag.service --question "How do I apply for CS at MIT?" --top_k 8

Programmatic:
    from college_ai.rag.service import CollegeRAG
    rag = CollegeRAG()
    result = rag.answer_question("Best scholarships for business majors?")
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from college_ai.rag.embeddings import get_embedding
from college_ai.rag.prompts import (
    ESSAY_IDEAS_SYSTEM,
    ESSAY_IDEAS_USER,
    ESSAY_REVIEW_SYSTEM,
    ESSAY_REVIEW_USER,
    NO_ANSWER_RESPONSE,
    QA_SYSTEM,
    QA_SYSTEM_MULTITURN,
    QA_USER,
    QUERY_REWRITE_SYSTEM,
    format_essay_prompt_context,
    format_experiences,
    format_profile_context,
    get_extra_instructions,
    get_essay_length_budget,
    get_length_budget,
)
from college_ai.rag.reranker import Reranker
from college_ai.rag.retrieval import HybridRetriever
from college_ai.rag.router import (
    ADMISSION_PREDICTION,
    ESSAY_IDEAS,
    ESSAY_REVIEW,
    QA,
    QueryRouter,
)
from college_ai.scraping.config import VECTOR_DIM

logger = logging.getLogger(__name__)

# Load .env
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


class CollegeRAG:
    """RAG engine v2 with hybrid search, cross-encoder reranking,
    and specialized generators for Q&A and essay assistance.
    """

    def __init__(
        self,
        collection_name: Optional[str] = None,
        generation_model: Optional[str] = None,
        rewrite_model: Optional[str] = None,
    ):
        self.generation_model = generation_model or os.getenv(
            "OPENAI_CHAT_MODEL", "gpt-4.1-mini"
        )
        self.rewrite_model = rewrite_model or "gpt-4.1-nano"

        self.retriever = HybridRetriever(collection_name=collection_name)
        self.reranker = Reranker()
        self.router = QueryRouter()

        # Expose collection name for the /config endpoint
        self.collection_name = self.retriever.collection_name

        self._chat_client = None

    # ---- OpenAI client ----

    def _get_chat_client(self):
        if self._chat_client is None:
            from openai import OpenAI
            api_key = os.getenv("OPENAI_API_KEY", "").strip().strip('"').strip("'")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY not found in environment.")
            self._chat_client = OpenAI(api_key=api_key)
        return self._chat_client

    # ---- Query rewriting ----

    def _rewrite_query(self, question: str, query_type: str) -> str:
        """Rewrite the user query for better retrieval.

        Always rewrites (no length threshold). Uses cheap nano model.
        """
        try:
            client = self._get_chat_client()
            response = client.chat.completions.create(
                model=self.rewrite_model,
                messages=[
                    {"role": "system", "content": QUERY_REWRITE_SYSTEM},
                    {"role": "user", "content": question},
                ],
                temperature=0,
                max_tokens=120,
            )
            if response and response.choices:
                rewritten = response.choices[0].message.content or ""
                return rewritten.strip() or question
        except Exception:
            pass
        return question

    # ---- Context formatting ----

    @staticmethod
    def _build_context_snippets(hits: List[Dict[str, Any]]) -> str:
        """Format retrieved hits as numbered source snippets for the prompt."""
        if not hits:
            return ""

        # Scale snippet length inversely with hit count
        if len(hits) <= 3:
            max_chars = 2500
        elif len(hits) <= 6:
            max_chars = 1800
        else:
            max_chars = 1500

        snippets = []
        for idx, rec in enumerate(hits, start=1):
            college = rec.get("college_name") or ""
            title = rec.get("title") or ""
            url = rec.get("url") or ""
            crawled_at = rec.get("crawled_at") or "unknown"
            content = (rec.get("content") or "").strip()
            if len(content) > max_chars:
                content = content[:max_chars] + "..."
            snippet = (
                f"[{idx}] {college} — {title} (crawled: {crawled_at})\n"
                f"URL: {url}\n{content}"
            )
            snippets.append(snippet)

        return "\n\n".join(snippets)

    # ---- Citation verification ----

    @staticmethod
    def _verify_citations(answer: str, num_sources: int) -> str:
        """Strip invalid citation references and warn if none remain."""
        if num_sources == 0:
            return answer

        def _replace_invalid(match: re.Match) -> str:
            cited = int(match.group(1))
            if cited < 1 or cited > num_sources:
                return ""
            return match.group(0)

        cleaned = re.sub(r"\[(\d+)\]", _replace_invalid, answer)

        valid = re.findall(r"\[\d+\]", cleaned)
        if not valid and num_sources > 0:
            cleaned += (
                "\n\n> **Note:** This response may not be fully grounded in the "
                "provided sources. Please verify with the college's official website."
            )

        return cleaned

    # ---- Confidence scoring ----

    @staticmethod
    def _compute_confidence(hits: List[Dict[str, Any]]) -> str:
        """Compute confidence label from source count and distances."""
        if not hits:
            return "low"

        distances = [float(h.get("distance", 0.0) or 0.0) for h in hits]
        avg = sum(distances) / len(distances)

        # For COSINE metric, higher = more similar (opposite of L2)
        # Reranked results may have rerank_score instead
        has_rerank = any("rerank_score" in h for h in hits)

        if has_rerank:
            scores = [float(h.get("rerank_score", 0.0) or 0.0) for h in hits]
            avg_score = sum(scores) / len(scores)
            if len(hits) >= 4 and avg_score > 0.5:
                return "high"
            if len(hits) >= 2 and avg_score > 0.2:
                return "medium"
            return "low"

        # COSINE similarity: 1.0 = identical, 0.0 = orthogonal
        if len(hits) >= 4 and avg > 0.6:
            return "high"
        if len(hits) >= 2 and avg > 0.4:
            return "medium"
        return "low"

    # ---- Generation: University Q&A ----

    def _generate_qa(
        self,
        question: str,
        hits: List[Dict[str, Any]],
        college_name: Optional[str],
    ) -> str:
        """Generate a grounded Q&A answer with citations."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        # Inject ML prediction context if applicable
        prediction_context = ""
        try:
            from college_ai.rag.bridge import get_prediction_context
            ctx = get_prediction_context(question, college_name=college_name)
            if ctx:
                prediction_context = f"\n{ctx}\n"
        except Exception:
            pass

        system = QA_SYSTEM
        if college_name:
            system += f"\nPrioritize information for {college_name} if present."

        user_prompt = QA_USER.format(
            question=question,
            sources_block=sources_block,
            prediction_context=prediction_context,
            extra_instructions=get_extra_instructions(question),
            length_budget=get_length_budget(question),
        )

        response = client.chat.completions.create(
            model=self.generation_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        if response and response.choices:
            return response.choices[0].message.content or ""
        return ""

    # ---- Streaming generation ----

    def _build_messages(
        self,
        question: str,
        query_type: str,
        hits: List[Dict[str, Any]],
        college_name: Optional[str],
        essay_text: Optional[str] = None,
        essay_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        experiences: Optional[List[Dict[str, Any]]] = None,
        response_length: Optional[str] = None,
        profile: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """Build the messages list for the OpenAI chat call."""
        sources_block = self._build_context_snippets(hits)
        experience_context = format_experiences(experiences)

        if query_type == ESSAY_IDEAS:
            school_context = ""
            if college_name:
                school_context = f"School of interest: **{college_name}**\n\n"
            essay_prompt_context = format_essay_prompt_context(essay_prompt)
            system = ESSAY_IDEAS_SYSTEM
            essay_budget = get_essay_length_budget(response_length)
            user_prompt = ESSAY_IDEAS_USER.format(
                question=question,
                essay_prompt_context=essay_prompt_context,
                school_context=school_context,
                experience_context=experience_context,
                sources_block=sources_block,
            )
            user_prompt += f"\nTarget total length: under {essay_budget}."
        elif query_type == ESSAY_REVIEW:
            school_context = ""
            if college_name:
                school_context = f"School of interest: **{college_name}**\n\n"
            essay_prompt_context = format_essay_prompt_context(essay_prompt)
            system = ESSAY_REVIEW_SYSTEM.format(
                essay_length_budget=get_essay_length_budget(response_length),
            )
            user_prompt = ESSAY_REVIEW_USER.format(
                question=question,
                essay_prompt_context=essay_prompt_context,
                school_context=school_context,
                experience_context=experience_context,
                essay_text=essay_text or "(No draft provided)",
                sources_block=sources_block,
            )
        else:
            # QA / admission_prediction
            prediction_context = ""
            try:
                from college_ai.rag.bridge import get_prediction_context
                ctx = get_prediction_context(question, college_name=college_name)
                if ctx:
                    prediction_context = f"\n{ctx}\n"
            except Exception:
                pass

            system = QA_SYSTEM
            if college_name:
                system += f"\nPrioritize information for {college_name} if present."
            if history:
                system += QA_SYSTEM_MULTITURN

            user_prompt = QA_USER.format(
                question=question,
                profile_context=format_profile_context(profile),
                sources_block=sources_block,
                prediction_context=prediction_context,
                extra_instructions=get_extra_instructions(question),
                length_budget=get_length_budget(question, response_length),
            )

        messages = [{"role": "system", "content": system}]  # type: List[Dict[str, str]]

        # Add conversation history for multi-turn
        if history:
            for msg in history[-6:]:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _get_temperature(self, query_type: str) -> float:
        if query_type == ESSAY_IDEAS:
            return 0.4
        if query_type == ESSAY_REVIEW:
            return 0.3
        return 0.2

    # ---- Generation: Essay Ideas ----

    def _generate_essay_ideas(
        self,
        question: str,
        hits: List[Dict[str, Any]],
        college_name: Optional[str],
    ) -> str:
        """Generate essay brainstorming suggestions grounded in sources."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        school_context = ""
        if college_name:
            school_context = f"School of interest: **{college_name}**\n\n"

        user_prompt = ESSAY_IDEAS_USER.format(
            question=question,
            essay_prompt_context=format_essay_prompt_context(None),
            school_context=school_context,
            experience_context="",
            sources_block=sources_block,
        )

        response = client.chat.completions.create(
            model=self.generation_model,
            messages=[
                {"role": "system", "content": ESSAY_IDEAS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,  # slightly more creative for brainstorming
        )
        if response and response.choices:
            return response.choices[0].message.content or ""
        return ""

    # ---- Generation: Essay Review ----

    def _generate_essay_review(
        self,
        question: str,
        essay_text: str,
        hits: List[Dict[str, Any]],
        college_name: Optional[str],
    ) -> str:
        """Generate coaching feedback on an essay draft."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        school_context = ""
        if college_name:
            school_context = f"School of interest: **{college_name}**\n\n"

        user_prompt = ESSAY_REVIEW_USER.format(
            question=question,
            essay_prompt_context=format_essay_prompt_context(None),
            school_context=school_context,
            experience_context="",
            essay_text=essay_text or "(No draft provided)",
            sources_block=sources_block,
        )

        response = client.chat.completions.create(
            model=self.generation_model,
            messages=[
                {"role": "system", "content": ESSAY_REVIEW_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
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
        essay_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run end-to-end RAG: route → retrieve → rerank → generate.

        Args:
            question: User question or essay request.
            top_k: Number of sources to use for generation.
            college_name: School from dropdown (takes precedence).
            essay_text: Pasted essay draft for review mode.

        Returns:
            Dict with: answer, sources, confidence, source_count, query_type.
        """
        # 1. Route
        classification = self.router.classify(question, essay_text)
        school = college_name or classification.detected_school
        query_type = classification.query_type

        # 2. Rewrite query for retrieval
        search_query = self._rewrite_query(question, query_type)

        # 3. Retrieve
        embedding = get_embedding(search_query)
        if embedding is None or len(embedding) != VECTOR_DIM:
            return {
                "answer": NO_ANSWER_RESPONSE,
                "sources": [],
                "confidence": "low",
                "source_count": 0,
                "query_type": query_type,
            }

        # For essay modes, run supplemental queries with page_type targeting
        if query_type in (ESSAY_IDEAS, ESSAY_REVIEW) and school:
            queries = [search_query]
            queries.append(f"{school} mission values what we look for in students")
            queries.append(f"{school} unique programs culture community")
            candidates = self.retriever.search_multi_query(
                queries, college_name=school,
                page_types=["about", "academics", "campus_life", "diversity", "outcomes"],
                top_k=30,
            )
        else:
            candidates = self.retriever.search(
                search_query, embedding, college_name=school, top_k=30,
            )

        if not candidates:
            logger.warning(
                "RAG retrieval returned 0 candidates for query=%r school=%r",
                search_query[:80], school,
            )
            return {
                "answer": NO_ANSWER_RESPONSE,
                "sources": [],
                "confidence": "low",
                "source_count": 0,
                "query_type": query_type,
            }

        # Log retrieval health
        content_lens = [len(c.get("content") or "") for c in candidates]
        empty_names = sum(1 for c in candidates if not c.get("college_name"))
        logger.info(
            "RAG candidates=%d, avg_content_len=%d, empty_college_names=%d, query=%r",
            len(candidates),
            sum(content_lens) // max(len(content_lens), 1),
            empty_names,
            search_query[:80],
        )

        # 4. Rerank
        hits = self.reranker.rerank(question, candidates, top_k=top_k)

        # 5. Generate
        if query_type == ESSAY_IDEAS:
            answer = self._generate_essay_ideas(question, hits, school)
        elif query_type == ESSAY_REVIEW:
            answer = self._generate_essay_review(
                question, essay_text or "", hits, school,
            )
        else:
            # QA and admission_prediction both use the QA generator
            # (admission_prediction gets ML context injected via bridge)
            answer = self._generate_qa(question, hits, school)

        # 6. Post-process
        answer = self._verify_citations(answer, len(hits))
        confidence = self._compute_confidence(hits)

        return {
            "answer": answer,
            "sources": hits,
            "confidence": confidence,
            "source_count": len(hits),
            "query_type": query_type,
        }

    # ---- Streaming high-level API ----

    def answer_question_stream(
        self,
        question: str,
        *,
        top_k: int = 8,
        response_length: Optional[str] = None,
        college_name: Optional[str] = None,
        essay_text: Optional[str] = None,
        essay_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        experiences: Optional[List[Dict[str, Any]]] = None,
        profile: Optional[Dict[str, Any]] = None,
    ):
        """Stream RAG answer as SSE events (generator of dicts).

        Yields dicts with a ``type`` key:
        - ``{"type": "token", "content": "..."}``
        - ``{"type": "sources", "sources": [...], "confidence": "...", "query_type": "..."}``
        - ``{"type": "done"}``
        - ``{"type": "error", "message": "..."}``
        """
        import json

        try:
            # 1. Route
            classification = self.router.classify(question, essay_text)
            school = college_name or classification.detected_school
            query_type = classification.query_type

            # 2. Rewrite
            search_query = self._rewrite_query(question, query_type)

            # 3. Retrieve
            embedding = get_embedding(search_query)
            if embedding is None or len(embedding) != VECTOR_DIM:
                yield {"type": "token", "content": NO_ANSWER_RESPONSE}
                yield {"type": "sources", "sources": [], "confidence": "low", "query_type": query_type}
                yield {"type": "done"}
                return

            if query_type in (ESSAY_IDEAS, ESSAY_REVIEW) and school:
                queries = [search_query]
                queries.append(f"{school} mission values what we look for in students")
                queries.append(f"{school} unique programs culture community")
                candidates = self.retriever.search_multi_query(
                    queries, college_name=school,
                    page_types=["about", "academics", "campus_life", "diversity", "outcomes"],
                    top_k=30,
                )
            else:
                candidates = self.retriever.search(
                    search_query, embedding, college_name=school, top_k=30,
                )

            if not candidates:
                yield {"type": "token", "content": NO_ANSWER_RESPONSE}
                yield {"type": "sources", "sources": [], "confidence": "low", "query_type": query_type}
                yield {"type": "done"}
                return

            # 4. Rerank
            hits = self.reranker.rerank(question, candidates, top_k=top_k)

            # 5. Build messages and stream generation
            messages = self._build_messages(
                question, query_type, hits, school,
                essay_text=essay_text,
                essay_prompt=essay_prompt,
                history=history,
                experiences=experiences,
                response_length=response_length,
                profile=profile,
            )

            client = self._get_chat_client()
            stream = client.chat.completions.create(
                model=self.generation_model,
                messages=messages,
                temperature=self._get_temperature(query_type),
                stream=True,
            )

            full_answer = []
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    full_answer.append(token)
                    yield {"type": "token", "content": token}

            # 6. Post-process and send metadata
            answer = "".join(full_answer)
            answer = self._verify_citations(answer, len(hits))
            confidence = self._compute_confidence(hits)

            yield {
                "type": "sources",
                "sources": hits,
                "confidence": confidence,
                "query_type": query_type,
            }
            yield {"type": "done"}

        except Exception as e:
            logger.exception("Streaming error: %s", e)
            yield {"type": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_sources_for_cli(sources: List[Dict[str, Any]]) -> str:
    lines = []
    for idx, s in enumerate(sources, start=1):
        college = s.get("college_name", "")
        title = s.get("title", "")
        url = s.get("url", "")
        lines.append(f"[{idx}] {college} — {title}\n    {url}")
    return "\n".join(lines)


def _main_cli(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="College RAG CLI v2")
    parser.add_argument("--question", required=True, help="User question")
    parser.add_argument("--top_k", type=int, default=8)
    parser.add_argument("--college", default=None, help="College name filter")
    parser.add_argument("--essay_text", default=None, help="Essay draft for review")
    args = parser.parse_args(argv)

    rag = CollegeRAG()
    result = rag.answer_question(
        args.question,
        top_k=args.top_k,
        college_name=args.college,
        essay_text=args.essay_text,
    )

    print(f"\n==== [{result.get('query_type', 'qa')}] Answer ====")
    print(result.get("answer", ""))
    print(f"\n==== Sources ({result.get('confidence', 'unknown')} confidence) ====")
    print(_format_sources_for_cli(result.get("sources", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main_cli())
