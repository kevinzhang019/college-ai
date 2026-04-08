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
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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
    COMPARISON_INSTRUCTIONS,
    RANKING_INSTRUCTIONS,
    format_essay_prompt_context,
    format_experiences,
    format_profile_context,
    get_extra_instructions,
    get_essay_length_budget,
    get_length_budget,
)
from college_ai.rag.classifier import classify_query
from college_ai.rag.reranker import Reranker
from college_ai.rag.retrieval import HybridRetriever
from college_ai.rag.school_data import (
    fetch_school_data_batch,
    fetch_school_data_by_categories,
    format_multi_school_data_block_by_categories,
    format_niche_grades_block,
    format_school_data_block_by_categories,
)
from college_ai.rag.router import (
    ADMISSION_PREDICTION,
    COMPARISON,
    ESSAY_IDEAS,
    ESSAY_REVIEW,
    GREETING,
    QA,
    RANKING,
    QueryRouter,
)
from college_ai.scraping.config import (
    RAG_HISTORY_LIMIT,
    RAG_HISTORY_REWRITE_CHARS,
    RAG_HISTORY_REWRITE_LIMIT,
    RAG_RETRIEVAL_TOP_K,
    VECTOR_DIM,
)

logger = logging.getLogger(__name__)

# Load .env
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# ---------------------------------------------------------------------------
# Retrieval candidate cache — avoids repeated Milvus round-trips for the
# same (query, school_filter) within a short window.
# ---------------------------------------------------------------------------

_RETRIEVAL_CACHE = OrderedDict()  # type: OrderedDict
_RETRIEVAL_CACHE_LOCK = threading.Lock()
_RETRIEVAL_CACHE_MAX = 256
_RETRIEVAL_CACHE_TTL = 300  # 5 minutes


def _retrieval_cache_key(query, college_names):
    # type: (str, Optional[List[str]]) -> str
    schools_part = "|".join(sorted(n.lower() for n in (college_names or [])))
    return f"{query.strip().lower()}::{schools_part}"


def _get_cached_candidates(key):
    # type: (str) -> Optional[List[Dict[str, Any]]]
    with _RETRIEVAL_CACHE_LOCK:
        if key in _RETRIEVAL_CACHE:
            ts, candidates = _RETRIEVAL_CACHE[key]
            if time.time() - ts < _RETRIEVAL_CACHE_TTL:
                _RETRIEVAL_CACHE.move_to_end(key)
                logger.debug("Retrieval cache hit for key=%s", key[:60])
                return candidates
            else:
                del _RETRIEVAL_CACHE[key]
    return None


def _set_cached_candidates(key, candidates):
    # type: (str, List[Dict[str, Any]]) -> None
    with _RETRIEVAL_CACHE_LOCK:
        _RETRIEVAL_CACHE[key] = (time.time(), candidates)
        if len(_RETRIEVAL_CACHE) > _RETRIEVAL_CACHE_MAX:
            _RETRIEVAL_CACHE.popitem(last=False)


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

    # Prompt cache keys for OpenAI — group by system prompt identity so
    # requests sharing the same static prefix route to the same cache.
    _PROMPT_CACHE_KEYS = {
        QA: "cole-qa",
        RANKING: "cole-qa",  # same QA_SYSTEM prompt
        COMPARISON: "cole-qa",
        ADMISSION_PREDICTION: "cole-qa",
        ESSAY_IDEAS: "cole-essay-ideas",
        ESSAY_REVIEW: "cole-essay-review",
    }

    def _select_model(self, query_type: str, complexity: str) -> str:
        """Pick the generation model based on query type and complexity."""
        if query_type == QA and complexity == "simple":
            return self.model_simple
        return self.model_standard

    @staticmethod
    def _get_type_instructions(query_type: str) -> str:
        """Return type-specific prompt instructions for QA_USER."""
        if query_type == RANKING:
            return RANKING_INSTRUCTIONS
        if query_type == COMPARISON:
            return COMPARISON_INSTRUCTIONS
        return ""

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
                            "You are Cole — a warm, upbeat college admissions advisor who loves helping "
                            "people figure out the college process. Respond to the greeting like a "
                            "friendly, approachable person would — cheerful and natural, not stiff. "
                            "Keep it to 1-2 sentences. Invite them to ask you anything about colleges, "
                            "essays, or applications."
                        ),
                    },
                    {"role": "user", "content": question},
                ],
                temperature=0.5,
                max_tokens=100,
                prompt_cache_key="cole-greeting",
            )
            if response and response.choices:
                return response.choices[0].message.content or ""
        except Exception:
            pass
        return "Hey! I'm Cole — think of me as your go-to for all things college admissions. What's on your mind?"

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
                    context_lines.append(f"{role}: {content[:RAG_HISTORY_REWRITE_CHARS]}")
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
                prompt_cache_key="cole-rewrite",
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
        # Strip leaked [SCHOOL DATA] tags that the LLM may reproduce
        answer = re.sub(r"\s*\[SCHOOL DATA\][^\n]*", "", answer)
        # Strip literal [N] placeholders the LLM emits when it can't find a source number
        answer = re.sub(r"\s*\[N\]", "", answer)

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

    # ---- School name formatting helpers ----

    @staticmethod
    def _format_college_focus(schools: List[str]) -> str:
        """Build the 'Focus on:' line for QA prompts."""
        if not schools:
            return ""
        if len(schools) == 1:
            return f"Focus on: **{schools[0]}**\n"
        joined = ", ".join(f"**{s}**" for s in schools[:-1])
        return f"Focus on: {joined} and **{schools[-1]}**\n"

    @staticmethod
    def _format_school_context(schools: List[str]) -> str:
        """Build the 'School(s) of interest:' line for essay prompts."""
        if not schools:
            return ""
        if len(schools) == 1:
            return f"School of interest: **{schools[0]}**\n\n"
        joined = ", ".join(f"**{s}**" for s in schools[:-1])
        return f"Schools of interest: {joined} and **{schools[-1]}**\n\n"

    # ---- Generation: University Q&A ----

    def _generate_qa(
        self,
        question: str,
        hits: List[Dict[str, Any]],
        schools: List[str],
        complexity: str = "complex",
        school_data_block: str = "",
        query_type: str = QA,
    ) -> str:
        """Generate a grounded Q&A answer with citations."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        # Inject ML prediction context if applicable (single-school only)
        prediction_context = ""
        if len(schools) == 1:
            try:
                from college_ai.rag.bridge import get_prediction_context
                ctx = get_prediction_context(question, college_name=schools[0])
                if ctx:
                    prediction_context = f"\n{ctx}\n"
            except Exception:
                pass

        system = QA_SYSTEM

        user_prompt = QA_USER.format(
            question=question,
            college_focus=self._format_college_focus(schools),
            profile_context="",
            school_data_block=school_data_block,
            sources_block=sources_block,
            prediction_context=prediction_context,
            type_instructions=self._get_type_instructions(query_type),
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
            prompt_cache_key="cole-qa",
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
        schools: List[str],
        essay_text: Optional[str] = None,
        essay_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        experiences: Optional[List[Dict[str, Any]]] = None,
        response_length: Optional[str] = None,
        profile: Optional[Dict[str, Any]] = None,
        school_data_block: str = "",
    ) -> List[Dict[str, str]]:
        """Build the messages list for the OpenAI chat call."""
        sources_block = self._build_context_snippets(hits)
        experience_context = format_experiences(experiences)
        # Residency determination uses first school (in-state/out-of-state)
        profile_context = format_profile_context(
            profile, college_name=schools[0] if schools else None,
        )

        if query_type == ESSAY_IDEAS:
            school_context = self._format_school_context(schools)
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
            school_context = self._format_school_context(schools)
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
            if len(schools) == 1:
                try:
                    from college_ai.rag.bridge import get_prediction_context
                    ctx = get_prediction_context(
                        question, college_name=schools[0], profile=profile,
                    )
                    if ctx:
                        prediction_context = f"\n{ctx}\n"
                except Exception:
                    pass

            system = QA_SYSTEM

            user_prompt = QA_USER.format(
                question=question,
                college_focus=self._format_college_focus(schools),
                profile_context=profile_context,
                school_data_block=school_data_block,
                sources_block=sources_block,
                prediction_context=prediction_context,
                type_instructions=self._get_type_instructions(query_type),
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
        schools: List[str],
        school_data_block: str = "",
    ) -> str:
        """Generate essay brainstorming suggestions grounded in sources."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        school_context = self._format_school_context(schools)

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
            prompt_cache_key="cole-essay-ideas",
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
        schools: List[str],
        school_data_block: str = "",
    ) -> str:
        """Generate coaching feedback on an essay draft."""
        client = self._get_chat_client()
        sources_block = self._build_context_snippets(hits)

        school_context = self._format_school_context(schools)

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
            prompt_cache_key="cole-essay-review",
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
        # 1. Pre-classify (rule-based short-circuits)
        pre = self.router.classify(question, essay_text)
        # Dropdown takes precedence — skip extraction entirely
        schools = [college_name] if college_name else pre.detected_schools

        # Short-circuit for greetings — skip the full RAG pipeline
        if pre.query_type == GREETING:
            answer = self._generate_greeting(question)
            return {
                "answer": answer,
                "sources": [],
                "confidence": "high",
                "source_count": 0,
                "query_type": GREETING,
                "reranked": False,
            }

        # 2. LLM classification (type + complexity + categories)
        intent = classify_query(question)
        query_type = pre.query_type or intent.query_type
        complexity = intent.complexity

        # Fetch school data and rewrite+embed in parallel — for non-ranking
        # queries the DB fetch is independent of the retrieval path.
        categories = list(dict.fromkeys(["identity"] + intent.categories))
        school_data_map = {}  # type: Dict[str, Dict[str, Any]]
        sd_block = ""
        sd_future = None

        need_school_data = query_type not in (RANKING, COMPARISON) and schools
        if need_school_data:
            pool = ThreadPoolExecutor(max_workers=1)
            if len(schools) == 1:
                sd_future = pool.submit(
                    fetch_school_data_by_categories, schools[0], categories,
                )
            else:
                sd_future = pool.submit(fetch_school_data_batch, schools)

        # 3. Rewrite query for retrieval (runs in parallel with school fetch)
        search_query = self._rewrite_query(question, query_type)

        # 4. Retrieve
        embedding = get_embedding(search_query)
        if embedding is None or len(embedding) != VECTOR_DIM:
            if sd_future:
                sd_future.cancel()
                pool.shutdown(wait=False)
            return {
                "answer": NO_ANSWER_RESPONSE,
                "sources": [],
                "confidence": "low",
                "source_count": 0,
                "query_type": query_type,
            }

        # Collect school data result from parallel fetch
        if sd_future:
            sd_result = sd_future.result()
            pool.shutdown(wait=False)
            if len(schools) == 1:
                if sd_result:
                    sd_block = format_school_data_block_by_categories(sd_result, categories)
                    school_data_map[sd_result["name"].lower()] = sd_result
            else:
                school_data_map = sd_result
                sd_block = format_multi_school_data_block_by_categories(
                    school_data_map, schools, categories,
                )

        # Check retrieval cache before hitting Milvus
        cache_key = _retrieval_cache_key(search_query, schools or None)
        candidates = _get_cached_candidates(cache_key)

        if candidates is None:
            # Multi-query retrieval for richer context coverage
            if query_type in (ESSAY_IDEAS, ESSAY_REVIEW) and schools:
                queries = [search_query]
                for s in schools[:2]:  # cap auxiliary queries at 2 schools
                    queries.append(f"{s} mission values what we look for in students")
                    queries.append(f"{s} unique programs culture community")
                candidates = self.retriever.search_multi_query(
                    queries, college_names=schools or None, top_k=RAG_RETRIEVAL_TOP_K,
                )
            elif query_type == ADMISSION_PREDICTION and schools:
                queries = [search_query]
                for s in schools[:2]:
                    queries.append(f"{s} admissions statistics acceptance rate class profile")
                    queries.append(f"{s} application requirements deadlines")
                candidates = self.retriever.search_multi_query(
                    queries, college_names=schools or None, top_k=RAG_RETRIEVAL_TOP_K,
                )
            else:
                candidates = self.retriever.search(
                    search_query, embedding, college_names=schools or None, top_k=RAG_RETRIEVAL_TOP_K,
                )
            _set_cached_candidates(cache_key, candidates)

        if not candidates:
            logger.warning(
                "RAG retrieval returned 0 candidates for query=%r schools=%r",
                search_query[:80], schools,
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

        # 5. Rerank
        if query_type in (RANKING, COMPARISON):
            candidate_names = list({
                c.get("college_name", "") for c in candidates if c.get("college_name")
            })
            school_data_map = fetch_school_data_batch(candidate_names)

        preferred_page_types = (
            ["about", "academics", "campus_life", "diversity", "outcomes",
             "admissions", "safety_health", "research"]
            if query_type in (ESSAY_IDEAS, ESSAY_REVIEW) else None
        )
        hits = self.reranker.rerank(
            question, candidates, top_k=top_k,
            intent=intent,
            school_data_map=school_data_map,
            preferred_page_types=preferred_page_types,
        )

        # 6. Build school data block for ranking/comparison queries
        if query_type in (RANKING, COMPARISON):
            hit_names = list(dict.fromkeys(
                (h.get("college_name") or "") for h in hits if h.get("college_name")
            ))
            sd_block = format_multi_school_data_block_by_categories(
                school_data_map, hit_names, categories,
            )
            # Ranking queries also get Niche grades for ordering context
            if query_type == RANKING:
                sd_block += format_niche_grades_block(
                    school_data_map, hits, intent.niche_categories,
                )

        # 7. Generate
        if query_type == ESSAY_IDEAS:
            answer = self._generate_essay_ideas(question, hits, schools, school_data_block=sd_block)
        elif query_type == ESSAY_REVIEW:
            answer = self._generate_essay_review(
                question, essay_text or "", hits, schools, school_data_block=sd_block,
            )
        else:
            # QA, admission_prediction, ranking, comparison all use the QA generator
            answer = self._generate_qa(question, hits, schools, complexity, school_data_block=sd_block, query_type=query_type)

        # 8. Post-process
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
            # 1. Pre-classify (rule-based short-circuits)
            pre = self.router.classify(question, essay_text, essay_prompt)
            # Dropdown takes precedence — skip extraction entirely
            schools = [college_name] if college_name else pre.detected_schools

            # Short-circuit for greetings — skip the full RAG pipeline
            if pre.query_type == GREETING:
                answer = self._generate_greeting(question)
                yield {"type": "token", "content": answer}
                yield {"type": "sources", "sources": [], "confidence": "high", "query_type": GREETING, "reranked": False}
                yield {"type": "done"}
                return

            # 2. LLM classification (type + complexity + categories)
            intent = classify_query(question)
            query_type = pre.query_type or intent.query_type
            complexity = intent.complexity

            # Fetch school data and rewrite+embed in parallel — for non-ranking
            # queries the DB fetch is independent of the retrieval path.
            categories = list(dict.fromkeys(["identity"] + intent.categories))
            school_data_map = {}  # type: Dict[str, Dict[str, Any]]
            sd_block = ""
            sd_future = None

            need_school_data = query_type not in (RANKING, COMPARISON) and schools
            if need_school_data:
                pool = ThreadPoolExecutor(max_workers=1)
                if len(schools) == 1:
                    sd_future = pool.submit(
                        fetch_school_data_by_categories, schools[0], categories,
                    )
                else:
                    sd_future = pool.submit(fetch_school_data_batch, schools)

            # 3. Rewrite (pass history so pronouns/references are resolved,
            # runs in parallel with school data fetch)
            search_query = self._rewrite_query(question, query_type, history=history)

            # 4. Retrieve
            embedding = get_embedding(search_query)
            if embedding is None or len(embedding) != VECTOR_DIM:
                if sd_future:
                    sd_future.cancel()
                    pool.shutdown(wait=False)
                yield {"type": "token", "content": NO_ANSWER_RESPONSE}
                yield {"type": "sources", "sources": [], "confidence": "low", "query_type": query_type}
                yield {"type": "done"}
                return

            # Collect school data result from parallel fetch
            if sd_future:
                sd_result = sd_future.result()
                pool.shutdown(wait=False)
                if len(schools) == 1:
                    if sd_result:
                        sd_block = format_school_data_block_by_categories(sd_result, categories)
                        school_data_map[sd_result["name"].lower()] = sd_result
                else:
                    school_data_map = sd_result
                    sd_block = format_multi_school_data_block_by_categories(
                        school_data_map, schools, categories,
                    )

            # Check retrieval cache before hitting Milvus
            cache_key = _retrieval_cache_key(search_query, schools or None)
            candidates = _get_cached_candidates(cache_key)

            if candidates is None:
                if query_type in (ESSAY_IDEAS, ESSAY_REVIEW) and schools:
                    queries = [search_query]
                    for s in schools[:2]:
                        queries.append(f"{s} mission values what we look for in students")
                        queries.append(f"{s} unique programs culture community")
                    candidates = self.retriever.search_multi_query(
                        queries, college_names=schools or None, top_k=RAG_RETRIEVAL_TOP_K,
                    )
                elif query_type == ADMISSION_PREDICTION and schools:
                    queries = [search_query]
                    for s in schools[:2]:
                        queries.append(f"{s} admissions statistics acceptance rate class profile")
                        queries.append(f"{s} application requirements deadlines")
                    candidates = self.retriever.search_multi_query(
                        queries, college_names=schools or None, top_k=RAG_RETRIEVAL_TOP_K,
                    )
                else:
                    candidates = self.retriever.search(
                        search_query, embedding, college_names=schools or None, top_k=RAG_RETRIEVAL_TOP_K,
                    )
                _set_cached_candidates(cache_key, candidates)

            if not candidates:
                yield {"type": "token", "content": NO_ANSWER_RESPONSE}
                yield {"type": "sources", "sources": [], "confidence": "low", "query_type": query_type}
                yield {"type": "done"}
                return

            # 5. Rerank
            if query_type in (RANKING, COMPARISON):
                candidate_names = list({
                    c.get("college_name", "") for c in candidates if c.get("college_name")
                })
                school_data_map = fetch_school_data_batch(candidate_names)

            preferred_page_types = (
                ["about", "academics", "campus_life", "diversity", "outcomes",
                 "admissions", "safety_health", "research"]
                if query_type in (ESSAY_IDEAS, ESSAY_REVIEW) else None
            )
            hits = self.reranker.rerank(
                question, candidates, top_k=top_k,
                intent=intent,
                school_data_map=school_data_map,
                preferred_page_types=preferred_page_types,
            )

            # 6. Build school data block for ranking/comparison queries
            if query_type in (RANKING, COMPARISON):
                hit_names = list(dict.fromkeys(
                    (h.get("college_name") or "") for h in hits if h.get("college_name")
                ))
                sd_block = format_multi_school_data_block_by_categories(
                    school_data_map, hit_names, categories,
                )
                # Ranking queries also get Niche grades for ordering context
                if query_type == RANKING:
                    sd_block += format_niche_grades_block(
                        school_data_map, hits, intent.niche_categories,
                    )

            # 7. Build messages and stream generation
            messages = self._build_messages(
                question, query_type, hits, schools,
                essay_text=essay_text,
                essay_prompt=essay_prompt,
                history=history,
                experiences=experiences,
                response_length=response_length,
                profile=profile,
                school_data_block=sd_block,
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
                prompt_cache_key=self._PROMPT_CACHE_KEYS.get(query_type, "cole-qa"),
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
