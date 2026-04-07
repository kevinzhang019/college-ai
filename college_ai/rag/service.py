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
    QA_USER,
    QUERY_REWRITE_SYSTEM,
    format_essay_prompt_context,
    format_experiences,
    format_profile_context,
    get_extra_instructions,
    get_essay_length_budget,
    get_length_budget,
)
from college_ai.rag.ranking import detect_ranking_intent
from college_ai.rag.reranker import Reranker
from college_ai.rag.retrieval import HybridRetriever
from college_ai.rag.school_data import fetch_school_data, format_school_data_block
from college_ai.rag.router import (
    ADMISSION_PREDICTION,
    ESSAY_IDEAS,
    ESSAY_REVIEW,
    GREETING,
    QA,
    SIMPLE,
    QueryRouter,
)
from college_ai.scraping.config import (
    RAG_HISTORY_LIMIT,
    RAG_HISTORY_REWRITE_LIMIT,
    VECTOR_DIM,
)

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
        # Tiered model selection: simple Q&A gets a cheap model,
        # everything else gets a higher-quality model.
        self.model_simple = os.getenv("MODEL_SIMPLE", "gpt-4.1-nano")
        self.model_standard = os.getenv("MODEL_STANDARD", "gpt-5.4-mini")

        # Legacy override: if OPENAI_CHAT_MODEL or generation_model is set,
        # use it as the standard model for backward compatibility.
        if generation_model:
            self.model_standard = generation_model
        elif os.getenv("OPENAI_CHAT_MODEL"):
            self.model_standard = os.getenv("OPENAI_CHAT_MODEL")

        self.rewrite_model = rewrite_model or "gpt-4.1-nano"

        self.retriever = HybridRetriever(collection_name=collection_name)
        self.reranker = Reranker()
        self.router = QueryRouter()

        # Expose collection name for the /config endpoint
        self.collection_name = self.retriever.collection_name

        self._chat_client = None

    def _select_model(self, query_type: str, complexity: str) -> str:
        """Pick the generation model based on query type and complexity."""
        if query_type == QA and complexity == SIMPLE:
            return self.model_simple
        return self.model_standard

    # ---- OpenAI client ----

    def _get_chat_client(self):
        if self._chat_client is None:
            from openai import OpenAI
            api_key = os.getenv("OPENAI_API_KEY", "").strip().strip('"').strip("'")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY not found in environment.")
            self._chat_client = OpenAI(api_key=api_key)
        return self._chat_client

    # ---- Greeting handler (no retrieval) ----

    def _generate_greeting(self, question: str) -> str:
        """Generate a lightweight response for greetings/off-topic without RAG."""
        try:
            client = self._get_chat_client()
            response = client.chat.completions.create(
                model=self.model_simple,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Cole, a friendly college admissions advisor. "
                            "Respond briefly to the greeting or conversational message. "
                            "Keep it to 1-2 sentences. Offer to help with college questions."
                        ),
                    },
                    {"role": "user", "content": question},
                ],
                temperature=0.5,
                max_tokens=100,
            )
            if response and response.choices:
                return response.choices[0].message.content or ""
        except Exception:
            pass
        return "Hey! I'm Cole, your college admissions advisor. What can I help you with?"

    # ---- Query rewriting ----

    def _rewrite_query(
        self,
        question: str,
        query_type: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Rewrite the user query for better retrieval.

        Always rewrites (no length threshold). Uses cheap nano model.
        When *history* is provided, includes the last few messages so the
        rewriter can resolve pronouns and implicit references (e.g.
        "What about their CS program?" → "MIT Computer Science program").
        """
        try:
            client = self._get_chat_client()

            # Build user content: optionally prepend recent conversation context
            user_content = question
            if history:
                recent = history[-RAG_HISTORY_REWRITE_LIMIT:]
                context_lines = []
                for msg in recent:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    # Truncate to keep rewrite fast
                    context_lines.append(f"{role}: {content[:200]}")
                user_content = (
                    "Recent conversation:\n"
                    + "\n".join(context_lines)
                    + f"\n\nCurrent question: {question}"
                )

            response = client.chat.completions.create(
                model=self.rewrite_model,
                messages=[
                    {"role": "system", "content": QUERY_REWRITE_SYSTEM},
                    {"role": "user", "content": user_content},
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
        """Compute confidence label from source count and relevance scores.

        Prefers rerank_score (Cohere, 0-1 higher = more relevant) when
        available. Falls back to RRF distance from hybrid search.
        """
        if not hits:
            return "low"

        # Prefer rerank scores (clear semantics: higher = more relevant)
        has_rerank = any("rerank_score" in h for h in hits)
        if has_rerank:
            scores = [float(h.get("rerank_score", 0.0) or 0.0) for h in hits]
            avg_score = sum(scores) / len(scores)
            if len(hits) >= 4 and avg_score > 0.5:
                return "high"
            if len(hits) >= 2 and avg_score > 0.2:
                return "medium"
            return "low"

        # RRF/hybrid search distance: higher score = more relevant
        # (RRF produces fused relevance scores, not cosine distances)
        distances = [float(h.get("distance", 0.0) or 0.0) for h in hits]
        avg = sum(distances) / len(distances)
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
        complexity: str = "complex",
        school_data: Optional[Dict[str, Any]] = None,
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

        school_data_block = ""
        if school_data:
            school_data_block = format_school_data_block(school_data)

        system = QA_SYSTEM

        user_prompt = QA_USER.format(
            question=question,
            college_focus=f"Focus on: **{college_name}**\n" if college_name else "",
            profile_context="",
            school_data_block=school_data_block,
            sources_block=sources_block,
            prediction_context=prediction_context,
            extra_instructions=get_extra_instructions(question),
            length_budget=get_length_budget(question),
        )

        model = self._select_model(QA, complexity)
        logger.info("Generation model=%s complexity=%s query=%r", model, complexity, question[:80])

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=self._get_max_tokens(None, QA),
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
        school_data: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """Build the messages list for the OpenAI chat call."""
        sources_block = self._build_context_snippets(hits)
        experience_context = format_experiences(experiences)
        profile_context = format_profile_context(profile, college_name=college_name)

        school_data_block = ""
        if school_data:
            school_data_block = format_school_data_block(school_data)

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
                school_data_block=school_data_block,
                experience_context=experience_context,
                sources_block=sources_block,
            )
            if profile_context:
                user_prompt += f"\n{profile_context}"
            user_prompt += f"\nTarget total length: under {essay_budget}."
        elif query_type == ESSAY_REVIEW:
            school_context = ""
            if college_name:
                school_context = f"School of interest: **{college_name}**\n\n"
            essay_prompt_context = format_essay_prompt_context(essay_prompt)
            # essay_length_budget goes in the user message (not system) to
            # preserve a static system-prompt prefix for OpenAI prompt caching.
            system = ESSAY_REVIEW_SYSTEM
            essay_budget = get_essay_length_budget(response_length)
            user_prompt = ESSAY_REVIEW_USER.format(
                question=question,
                essay_prompt_context=essay_prompt_context,
                school_context=school_context,
                school_data_block=school_data_block,
                experience_context=experience_context,
                essay_text=essay_text or "(No draft provided)",
                sources_block=sources_block,
            )
            if profile_context:
                user_prompt += f"\n{profile_context}"
            user_prompt += f"\nKeep total feedback under {essay_budget}."
        else:
            # QA / admission_prediction
            prediction_context = ""
            try:
                from college_ai.rag.bridge import get_prediction_context
                ctx = get_prediction_context(
                    question, college_name=college_name, profile=profile,
                )
                if ctx:
                    prediction_context = f"\n{ctx}\n"
            except Exception:
                pass

            system = QA_SYSTEM

            user_prompt = QA_USER.format(
                question=question,
                college_focus=f"Focus on: **{college_name}**\n" if college_name else "",
                profile_context=profile_context,
                school_data_block=school_data_block,
                sources_block=sources_block,
                prediction_context=prediction_context,
                extra_instructions=get_extra_instructions(question),
                length_budget=get_length_budget(question, response_length),
            )

        # Multi-turn instructions are now baked into each system prompt
        # (static prefix) so OpenAI prompt caching works across single-turn
        # and multi-turn requests.

        messages = [{"role": "system", "content": system}]  # type: List[Dict[str, str]]

        # Add conversation history for multi-turn
        if history:
            for msg in history[-RAG_HISTORY_LIMIT:]:
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

    @staticmethod
    def _get_max_tokens(response_length: Optional[str], query_type: str) -> int:
        """Return a hard max_tokens cap for the generation call."""
        _LENGTH_TOKEN_MAP = {
            "XS": 200,
            "S": 400,
            "M": 700,
            "L": 1200,
            "XL": 1800,
        }
        if response_length and response_length in _LENGTH_TOKEN_MAP:
            return _LENGTH_TOKEN_MAP[response_length]
        # Default caps per query type
        if query_type in (ESSAY_IDEAS, ESSAY_REVIEW):
            return 1200
        return 700

    # ---- Generation: Essay Ideas ----

    def _generate_essay_ideas(
        self,
        question: str,
        hits: List[Dict[str, Any]],
        college_name: Optional[str],
        school_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate essay brainstorming suggestions grounded in sources."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        school_context = ""
        if college_name:
            school_context = f"School of interest: **{college_name}**\n\n"

        school_data_block = ""
        if school_data:
            school_data_block = format_school_data_block(school_data)

        user_prompt = ESSAY_IDEAS_USER.format(
            question=question,
            essay_prompt_context=format_essay_prompt_context(None),
            school_context=school_context,
            school_data_block=school_data_block,
            experience_context="",
            sources_block=sources_block,
        )

        response = client.chat.completions.create(
            model=self.model_standard,
            messages=[
                {"role": "system", "content": ESSAY_IDEAS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,  # slightly more creative for brainstorming
            max_tokens=self._get_max_tokens(None, ESSAY_IDEAS),
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
        school_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate coaching feedback on an essay draft."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        school_context = ""
        if college_name:
            school_context = f"School of interest: **{college_name}**\n\n"

        school_data_block = ""
        if school_data:
            school_data_block = format_school_data_block(school_data)

        user_prompt = ESSAY_REVIEW_USER.format(
            question=question,
            essay_prompt_context=format_essay_prompt_context(None),
            school_context=school_context,
            school_data_block=school_data_block,
            experience_context="",
            essay_text=essay_text or "(No draft provided)",
            sources_block=sources_block,
        )

        review_user_prompt = user_prompt + "\nKeep total feedback under 350 words."
        response = client.chat.completions.create(
            model=self.model_standard,
            messages=[
                {"role": "system", "content": ESSAY_REVIEW_SYSTEM},
                {"role": "user", "content": review_user_prompt},
            ],
            temperature=0.3,
            max_tokens=self._get_max_tokens(None, ESSAY_REVIEW),
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

        # Short-circuit for greetings — skip the full RAG pipeline
        if query_type == GREETING:
            answer = self._generate_greeting(question)
            return {
                "answer": answer,
                "sources": [],
                "confidence": "high",
                "source_count": 0,
                "query_type": query_type,
                "reranked": False,
            }

        # Fetch school data from DB (once — reused for prompt + reranking)
        school_data = None  # type: Optional[Dict[str, Any]]
        if school:
            school_data = fetch_school_data(school)

        # Detect ranking intent (LLM-based, gpt-4.1-nano)
        ranking_intent = detect_ranking_intent(question)

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

        # Multi-query retrieval for richer context coverage
        if query_type in (ESSAY_IDEAS, ESSAY_REVIEW) and school:
            queries = [search_query]
            queries.append(f"{school} mission values what we look for in students")
            queries.append(f"{school} unique programs culture community")
            candidates = self.retriever.search_multi_query(
                queries, college_name=school,
                page_types=["about", "academics", "campus_life", "diversity", "outcomes"],
                top_k=30,
            )
        elif query_type == ADMISSION_PREDICTION and school:
            queries = [search_query]
            queries.append(f"{school} admissions statistics acceptance rate class profile")
            queries.append(f"{school} application requirements deadlines")
            candidates = self.retriever.search_multi_query(
                queries, college_name=school, top_k=30,
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
        school_data_map = {}  # type: Dict[str, Dict[str, Any]]
        if school_data:
            school_data_map[school_data["name"].lower()] = school_data
        hits = self.reranker.rerank(
            question, candidates, top_k=top_k,
            ranking_intent=ranking_intent,
            school_data_map=school_data_map,
        )

        # 5. Generate
        complexity = classification.complexity
        if query_type == ESSAY_IDEAS:
            answer = self._generate_essay_ideas(question, hits, school, school_data=school_data)
        elif query_type == ESSAY_REVIEW:
            answer = self._generate_essay_review(
                question, essay_text or "", hits, school, school_data=school_data,
            )
        else:
            # QA and admission_prediction both use the QA generator
            # (admission_prediction gets ML context injected via bridge)
            answer = self._generate_qa(question, hits, school, complexity, school_data=school_data)

        # 6. Post-process
        answer = self._verify_citations(answer, len(hits))
        confidence = self._compute_confidence(hits)

        return {
            "answer": answer,
            "sources": hits,
            "confidence": confidence,
            "source_count": len(hits),
            "query_type": query_type,
            "reranked": self.reranker.available,
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

            # Short-circuit for greetings — skip the full RAG pipeline
            if query_type == GREETING:
                answer = self._generate_greeting(question)
                yield {"type": "token", "content": answer}
                yield {"type": "sources", "sources": [], "confidence": "high", "query_type": query_type, "reranked": False}
                yield {"type": "done"}
                return

            # Fetch school data from DB (once — reused for prompt + reranking)
            school_data = None  # type: Optional[Dict[str, Any]]
            if school:
                school_data = fetch_school_data(school)

            # Detect ranking intent (LLM-based, gpt-4.1-nano)
            ranking_intent = detect_ranking_intent(question)

            # 2. Rewrite (pass history so pronouns/references are resolved)
            search_query = self._rewrite_query(question, query_type, history=history)

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
            elif query_type == ADMISSION_PREDICTION and school:
                queries = [search_query]
                queries.append(f"{school} admissions statistics acceptance rate class profile")
                queries.append(f"{school} application requirements deadlines")
                candidates = self.retriever.search_multi_query(
                    queries, college_name=school, top_k=30,
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
            school_data_map = {}  # type: Dict[str, Dict[str, Any]]
            if school_data:
                school_data_map[school_data["name"].lower()] = school_data
            hits = self.reranker.rerank(
                question, candidates, top_k=top_k,
                ranking_intent=ranking_intent,
                school_data_map=school_data_map,
            )

            # 5. Build messages and stream generation
            complexity = classification.complexity
            messages = self._build_messages(
                question, query_type, hits, school,
                essay_text=essay_text,
                essay_prompt=essay_prompt,
                history=history,
                experiences=experiences,
                response_length=response_length,
                profile=profile,
                school_data=school_data,
            )

            model = self._select_model(query_type, complexity)
            logger.info("Streaming model=%s complexity=%s type=%s query=%r", model, complexity, query_type, question[:80])

            client = self._get_chat_client()
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=self._get_temperature(query_type),
                max_tokens=self._get_max_tokens(response_length, query_type),
                stream=True,
            )

            full_answer = []
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    full_answer.append(token)
                    yield {"type": "token", "content": token}

            # 6. Post-process and send metadata
            raw_answer = "".join(full_answer)
            verified_answer = self._verify_citations(raw_answer, len(hits))
            confidence = self._compute_confidence(hits)

            # If citation verification changed the answer (stripped invalid
            # refs or appended a warning), send the corrected text so the
            # frontend can replace what was streamed.
            if verified_answer != raw_answer:
                yield {"type": "answer_replaced", "content": verified_answer}

            yield {
                "type": "sources",
                "sources": hits,
                "confidence": confidence,
                "query_type": query_type,
                "reranked": self.reranker.available,
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
