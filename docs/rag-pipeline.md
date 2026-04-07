# RAG Pipeline v2

`rag/service.py` → `CollegeRAG` orchestrator

## Architecture

```
User Query + (optional school) + (optional history) + (optional experiences) + (optional profile w/ location)
    │
    ▼
[Router]  ─── classify: qa | essay_ideas | essay_review | admission_prediction | greeting
    │           + extract school name from query text (alias/acronym → substring → fuzzy)
    │           + complexity: simple | complex (for model routing)
    │           greeting → skip RAG pipeline, lightweight nano response
    ▼
[School Data + Ranking Intent]  ─── parallel, after routing:
    │   • fetch_school_data(school) — fuzzy-match → Turso DB (schools + niche_grades)
    │   • detect_ranking_intent(question) — gpt-4.1-nano classification
    │     → is_ranking + categories (academics, value, diversity, campus, etc.)
    ▼
[Query Rewriting]  ─── always rewrites via gpt-4.1-nano (no length threshold)
    │                     resolves pronouns/references using conversation history
    ▼
[Hybrid Retrieval]  ─── dense (COSINE) + BM25 via Milvus 2.5
    │                     pre-filter by college_name if specified
    │                     multi-query: essay (values, culture), admission (stats, reqs)
    │                     embedding cache: LRU 1024 entries (skips API on repeat queries)
    ▼
[Reranking]  ─── Cohere rerank-v3.5 cross-encoder (30 → 8 candidates)
    │              relevance threshold filter (score ≥ 0.1)
    │              ranking boost: if is_ranking, boost by niche_rank + category grades
    │              graceful fallback if COHERE_API_KEY not set
    ▼
[Model Selection]  ─── two-tier routing based on query type + complexity:
    │                    simple Q&A → gpt-4.1-nano (cheap, fast)
    │                    everything else → gpt-5.4-mini (higher quality, 90% cache discount)
    ▼
[Generator]  ─── route to specialized prompt:
    │              • QA: grounded answer with citations         (temp 0.2)
    │              • Essay Ideas: 3-4 brainstorming angles      (temp 0.4)
    │              • Essay Review: coaching feedback on draft    (temp 0.3)
    │              • Admission Prediction: QA + ML bridge       (temp 0.2)
    │              school data block injected into user prompt (all modes)
    │
    ├── /ask (sync):     full response returned
    └── /ask/stream:     tokens via SSE → citation correction → sources/metadata
    │
    ▼
[Post-processing]  ─── citation verification + confidence scoring
    │
    ▼
{answer, sources, confidence, source_count, query_type, reranked}
```

## Modules

| File | Purpose |
|---|---|
| `rag/router.py` | Two-layer query classifier (rule-based + LLM fallback) + school name extraction (alias/acronym dict → substring → rapidfuzz) + complexity classification for model routing + greeting detection (skips RAG pipeline) |
| `rag/school_data.py` | Fetches school + Niche grade data from Turso DB via `SchoolMatcher` fuzzy matching. `fetch_school_data()` for single school, `fetch_school_data_batch()` for multi-school ranking queries. Formats as `[SCHOOL DATA]` block: base stats only for non-ranking, category-specific Niche grades for ranking |
| `rag/ranking.py` | LLM-based ranking intent detection (gpt-4.1-nano). Classifies whether a query is a ranking and categorizes into Niche categories (supports multiple categories) for reranking boost |
| `rag/retrieval.py` | `HybridRetriever` — dense + BM25 hybrid search, school pre-filtering, URL dedup |
| `rag/reranker.py` | Cohere rerank-v3.5 wrapper with graceful degradation, ranking boost (niche_rank + category grades + acceptance rate), exposes `available` property for response metadata |
| `rag/prompts.py` | All system/user prompts: QA, essay ideas, essay review, query rewriting, classification. Also `format_experiences()`, `format_profile_context()`, `determine_residency()`, `get_extra_instructions()` (conditional domain knowledge). Multi-turn instructions are inlined into each system prompt for prompt caching. |
| `rag/service.py` | Thin orchestrator wiring router → school data fetch + ranking detection → retrieval → reranker → generator |
| `rag/bridge.py` | ML prediction injection for admission-probability questions |
| `rag/embeddings.py` | OpenAI embedding utilities, batch processing, cross-thread batcher, in-memory embedding cache (LRU 1024), sentence-aware chunking |
| `rag/text_cleaner.py` | HTML cleaning, content extraction, dedup |

## Step 1: Query Routing (`router.py`)

Two-layer classifier:

**Layer 1 — Rule-based (zero latency, ~85% of queries):**
- **Greeting detection** (checked first): short messages (≤8 words) with no factual/essay signals that match greeting patterns ("hi", "hello", "thanks", "good morning", etc.) → `greeting` type, skips the entire RAG pipeline
- Essay signals: "essay", "common app", "personal statement", "help me write", "brainstorm", etc.
- Essay review signals: "review my", "feedback on", "edit my", etc.
- Factual signals: "acceptance rate", "deadline", "tuition", "financial aid", "dorm", "net price", "demonstrated interest", "css profile", "waitlist", "ap credit", etc.
- Admission prediction: regex patterns from `bridge.py` ("what are my chances", "can i get into", etc.)
- If `essay_text` param provided → always `essay_review`

**Layer 2 — LLM fallback (for ambiguous queries):**
- Single gpt-4.1-nano call, `max_tokens=10`, `temperature=0`
- Categories: `qa | essay_ideas | essay_review | admission_prediction | greeting`

**Complexity classification (for model routing):**
- Only Q&A queries can be "simple" — all other types are always "complex"
- Simple: < 20 words AND no comparison/strategy keywords AND at most 1 factual signal
- Complex: everything else (multiple signals, comparisons, how-to, strategy, long questions, LLM-classified)
- Conservative: ambiguous queries default to "complex" (better model)

**School extraction:**
- Alias/acronym lookup first: ~100 entries mapping acronyms (MIT, BYU, UCLA, UIUC, etc.), shorthands (UMich, WashU, UPenn, Cal Poly, Ole Miss, etc.), and single-name schools (Harvard, Stanford, etc.) to canonical names. Uses word-boundary regex to avoid false positives (e.g., "bu" inside "about").
- Exact substring match against known college list (from CSVs), longest match first
- Fuzzy ngram matching via rapidfuzz (`token_sort_ratio`, cutoff 85, ngram length 1-7)
- Dropdown selection takes precedence over text extraction
- When a school is detected from the prompt, it filters retrieval identically to a dropdown selection

## Step 2: Query Rewriting

Always rewrites (no 60-char threshold). Uses gpt-4.1-nano with a prompt optimized for college admissions semantic search. Expands abbreviations (CS, EA, ED, RD, FA, FAFSA).

**History-aware rewriting:** When conversation history is available (streaming path), the last N messages (configurable via `RAG_HISTORY_REWRITE_LIMIT`, default 3) are included in the rewrite prompt so the model can resolve pronouns and implicit references (e.g. "What about their CS program?" after asking about MIT → "MIT Computer Science program admissions requirements").

## Step 3: Hybrid Retrieval (`retrieval.py`)

Uses ORM `Collection.hybrid_search()` with two arms:

| Arm | Field | Metric | Notes |
|---|---|---|---|
| Dense | `embedding` (FLOAT_VECTOR 1536) | COSINE | OpenAI text-embedding-3-small, nprobe configurable via `RETRIEVAL_NPROBE` (default 64) |
| Sparse | `content_sparse` (SPARSE_FLOAT_VECTOR) | BM25 | Auto-generated by Milvus from `content` field |

**Fusion:** `RRFRanker(k=60)` — Reciprocal Rank Fusion, parameter-free.

**School filtering:**
- If school specified: pre-filter via `expr='college_name == "X"'` on both arms
- If < 4 results from school filter: fall back to global search + soft score boost (+0.15) for the target school, sorted descending (higher RRF score = more relevant)
- INVERTED scalar index on `college_name` for millisecond filtering

**Embedding cache:** In-memory LRU cache (1024 entries, thread-safe) on `get_embedding()` keyed by text hash. Eliminates redundant OpenAI API calls for repeated/similar queries.

**Multi-query retrieval:** Multiple query types use supplemental queries for richer context:

| Query Type | Supplemental Queries | Page Type Filter |
|---|---|---|
| `essay_ideas` / `essay_review` | `{school} mission values what we look for in students`, `{school} unique programs culture community` | about, academics, campus_life, diversity, outcomes |
| `admission_prediction` | `{school} admissions statistics acceptance rate class profile`, `{school} application requirements deadlines` | (none) |

Results are merged and deduped by URL.

**URL diversity:** Max 2 chunks per URL (`MAX_CHUNKS_PER_URL`).

## Step 3.5: School Data Fetch + Ranking Intent Detection (`school_data.py` + `ranking.py`)

After routing (and before retrieval), two operations run:

**School data fetch** (`school_data.py`): When a school is detected (from query text or dropdown), `fetch_school_data(school_name)` fuzzy-matches the name via `SchoolMatcher` (from `ml/school_matcher.py`) → UNITID, then queries the `schools` + `niche_grades` tables from Turso. Returns a flat dict with all fields (admissions stats, financials, demographics, Niche grades, etc.) or None if no match. The `SchoolMatcher` instance is cached as a lazy module-level singleton to avoid reloading ~6,500 schools on every call.

For ranking queries, `fetch_school_data_batch(school_names)` batch-fetches data for all unique schools in the retrieval results (deduplicating by UNITID).

**School data injection varies by query type:**

- **Non-ranking queries:** `format_school_data_block(data)` renders a `[SCHOOL DATA]` block with **base stats only** (location, admissions, enrollment, financials, demographics) — **no Niche grades**. Single school.
- **Ranking queries:** `format_ranking_school_data_block(school_data_map, hits, categories)` renders a `[SCHOOL DATA]` block for **every school** in the reranked hits, with base stats plus **only the Niche grades matching the detected categories** (e.g. if categories=["academics", "athletics"], only those two grades appear per school). niche_rank is included in the header. If categories=["other"], no Niche grades are included (base stats only for all schools).

The LLM is instructed that `[SCHOOL DATA]` statistics can be referenced without citation (they come from our verified database, not crawled sources).

**Ranking intent detection** (`ranking.py`): `detect_ranking_intent(question)` uses a gpt-4.1-nano call (~$0.00003, ~200-400ms) with a static system prompt to classify whether the question is a ranking query and which Niche categories it maps to. Returns a `RankingIntent(is_ranking, categories)`.

Categories (matching `niche_grades` columns): `academics`, `value`, `diversity`, `campus`, `athletics`, `party_scene`, `professors`, `location`, `dorms`, `food`, `student_life`, `safety`, `other`.

Examples:
- "What are the best engineering schools?" → `is_ranking=True, categories=["academics"]`
- "Which school has the best social scene near a city?" → `is_ranking=True, categories=["party_scene", "location"]`
- "Tell me about MIT" → `is_ranking=False`
- "Compare MIT vs Stanford" → `is_ranking=False` (comparison, not ranking)

Falls back to `RankingIntent(is_ranking=False)` on any error.

## Step 4: Reranking (`reranker.py`)

Cohere `rerank-v3.5` cross-encoder. Takes 30 candidates from retrieval, returns top `top_k` (default 8, configurable 1–20 via API).

The frontend exposes this as a **context size selector** (XS=3, S=5, M=8, L=12, XL=16) in the input area, allowing users to trade speed for thoroughness.

- Documents sent as `"{title}\n{content[:3000]}"` (rerank-v3.5 supports ~4096 tokens)
- **Relevance threshold:** Hits with rerank_score < 0.1 are filtered out to avoid diluting context with irrelevant passages
- Falls back to retrieval order if `COHERE_API_KEY` not set or API fails
- ~200ms latency for 30 documents
- `top_k` parameter controls final count: XS(3), S(5), M(8, default), L(12), XL(16)

### Ranking Boost

For ranking queries (`ranking_intent.is_ranking == True`), after Cohere reranking but before the relevance threshold filter, `_apply_ranking_boost()` modifies scores:

| Boost Component | Weight | Formula | Condition |
|---|---|---|---|
| **Niche rank** | 0.15 | `max(0, 1 - (niche_rank - 1) / 500)` | Skipped if categories == `["other"]` |
| **Acceptance rate** | 0.05 | `1 - acceptance_rate` | Only if `"academics"` in categories |
| **Category grades** | 0.10 | Average of letter-grade-to-numeric across requested categories, normalized to 0-1 | Skipped if categories == `["other"]` |

Grade conversion: A+=4.3, A=4.0, A-=3.7, B+=3.3, B=3.0, B-=2.7, C+=2.3, C=2.0, C-=1.7, D+=1.3, D=1.0, D-=0.7, F=0. Normalized by dividing by 4.3.

Total max boost: ~0.30 on top of Cohere's 0-1 scores. Enough to influence ordering for ranking queries without overriding semantic relevance for non-ranking queries. Hits are re-sorted by boosted score after applying.

School data for the boost is passed via `school_data_map` (dict keyed by lowercased school name). For non-ranking queries, no boost is applied.

## Step 5: Generation (`prompts.py` + `service.py`)

### Model Routing

Two-tier model selection based on query type and complexity:

| Query Type | Complexity | Model | Cost/query |
|-----------|-----------|-------|-----------|
| Simple factual Q&A | simple | `gpt-4.1-nano` | ~$0.0005 |
| Complex Q&A, essay ideas, essay review, admission predictions | complex | `gpt-5.4-mini` | ~$0.004 |

Configurable via environment variables: `MODEL_SIMPLE` (default: `gpt-4.1-nano`), `MODEL_STANDARD` (default: `gpt-5.4-mini`). Legacy `OPENAI_CHAT_MODEL` overrides `MODEL_STANDARD` for backward compatibility.

### Prompt Caching

All system prompts share a `COLE_PREAMBLE` prefix (~950 tokens) containing the Cole persona, grounding contract, citation protocol, formatting rules, residency/statistics contextualization, essay coaching principles, and tone guidelines. Each mode appends its specific instructions plus multi-turn conversation handling (static text), pushing all system prompts above OpenAI's 1024-token caching threshold.

**Cache behavior:** OpenAI automatically caches identical prompt prefixes. The gpt-5.4-mini model gets a **90% cache discount** on cached tokens (vs 75% for the 4.1 family). Since the preamble is identical across all query types, it stays cached across requests regardless of mode. Multi-turn instructions are baked into the static system prompt (not dynamically appended) to preserve cache hits.

**Key constraints:**
- No variable content (school names, timestamps) in system prompts — `college_name` injection moved to user message as `{college_focus}`
- Essay review length budget moved to user message (was previously `.format()`-ed into system prompt, breaking cache)
- Multi-turn instructions inlined into each system prompt as static text (previously appended conditionally via `SYSTEM_MULTITURN`)

### Prompt Sets

All prompts use the **Cole** persona ("You are Cole, a college admissions advisor and essay coach"). Four specialized prompt sets, all enforcing citation grounding:

**QA:** Strict grounding contract — every factual claim needs `[N]` citation. Dynamic length budget based on query type (150-600 words), overridable by `response_length` parameter (XS: 50-100w, S: 100-200w, M: auto-detect, L: 400-600w, XL: 600-900w). Includes a statistics contextualization directive (acceptance rates describe the applicant pool, not individual chances; compare to student's profile when available). Optional "Next Steps" section for actionable queries. Conditional domain knowledge injected via `get_extra_instructions()` (see below). Temperature 0.2.

**Essay Ideas:** Coach persona. Identifies 3-4 specific programs/values/traditions from sources, suggests essay angles with hooks. Requires framing each angle as what the student BRINGS to the school, not what the school offers. Specificity rule: every suggestion must include a detail that could NOT apply to a different school. Does NOT write the essay. Temperature 0.4.

**Essay Review:** Coach persona reviewing a draft. Identifies strengths (naming the exact sentence/phrase that works), suggests school-specific details from sources, asks deepening questions, fact-checks claims, and flags common pitfalls (essay focuses on what school offers vs. what student contributes, inflated vocabulary, wrong school name, too much dialogue without reflection). Word cap overridable by `response_length` (XS: 150w, S: 250w, M: 350w default, L: 500w, XL: 700w). Temperature 0.3.

**Admission Prediction:** Uses QA prompt with ML prediction context injected via `bridge.py` (probability, CI, classification, key factors, plus strategic guidance: SAFETY/MATCH/REACH classification, actionable improvement suggestions for REACH schools). The bridge now accepts profile data as a fallback — if stats aren't in the question text but are in the user's saved profile (GPA, SAT/ACT), those are used for the prediction.

**Generation limits:** All generation calls set `max_tokens` based on `response_length` (XS: 200, S: 400, M: 700, L: 1200, XL: 1800) or query type defaults (essay modes: 1200, QA: 700).

### Multi-turn Conversation Support

Multi-turn instructions are baked into each system prompt (QA, essay ideas, essay review) as static text. When `history` is provided (via `/ask/stream`), the last N messages (configurable via `RAG_HISTORY_LIMIT`, default 6) are prepended to the messages list before the current user prompt. The model uses conversation context for follow-up questions, answers independently for new topics, and asks brief clarifying questions when the student's request is ambiguous or missing key details.

### Experience Context Injection

For `essay_ideas` and `essay_review` modes, `format_experiences(experiences)` converts the user's extracurricular list into a markdown block inserted into the user prompt as `{experience_context}`. Each experience renders as `- **Title** at Organization (type) [dates]` with an indented description. This enables personalized brainstorming grounded in both school sources and the student's actual activities.

### School Data Context Injection

Injects a `[SCHOOL DATA]` block into the user prompt via the `{school_data_block}` placeholder. Content varies by query type:

**Non-ranking queries** (single school detected): Base stats only from the `schools` table — no Niche grades.
- **Location & type:** city, state, ownership (public/private)
- **Admissions:** acceptance rate, SAT avg/range, ACT range
- **Enrollment & outcomes:** enrollment, student-faculty ratio, graduation rate, retention rate
- **Financials:** tuition (in-state/out-of-state), avg net cost, median earnings (10yr)
- **Demographics:** racial breakdown, first-gen percentage

**Ranking queries**: A `[SCHOOL DATA]` block for **each school** in the reranked hits, with base stats plus **only the Niche grades matching the detected categories**. For example, if `categories=["academics", "athletics"]`, each school's block includes only `Academics: A+ | Athletics: B+`. niche_rank is shown in the header. If `categories=["other"]`, no Niche grades are included.

The LLM is instructed that these statistics can be referenced without `[N]` citations since they come from our verified database rather than crawled sources. Fields that are None are omitted from the block.

For ranking queries, school data is batch-fetched via `fetch_school_data_batch()` after reranking (to cover all schools in the hits). For non-ranking queries, a single `fetch_school_data()` call is made early and reused.

### Profile Context Injection

For **all modes** (QA, admission prediction, essay ideas, essay review), `format_profile_context(profile, college_name)` converts the student's academic profile, location, and major preferences into a one-line context string inserted into the user prompt as `{profile_context}`. Example: `"Student profile: GPA 3.8, SAT 1450, Residency: in-state, State: CA, Preferred majors (ranked): #1 Computer Science, #2 Data Science"`. This enables the LLM to contextualize statistics against the student's actual credentials, personalize tuition/aid advice based on residency status, tailor program advice to the student's ranked major preferences, and personalize essay suggestions to the student's academic profile.

**Residency determination** (`determine_residency(profile, college_name)`): When the student has set their country/state and a school is selected, this function fuzzy-matches the school name against the Turso DB via `SchoolMatcher`, retrieves the school's state, and compares:
- Non-US country → `"international"`
- US, same state as school → `"in-state"`
- US, different state → `"out-of-state"`
- Insufficient data → `None` (omitted from prompt)

Profile data flows from the frontend Zustand store (`profile: { gpa, testScoreType, testScore, country, countryLabel, state, preferredMajors }`) → `useStreaming` hook (sent on every request when GPA, country, or preferred majors are set) → `/ask/stream` `profile` field → `_build_messages()` → `format_profile_context(profile, college_name)`.

### Conditional Domain Instructions (`get_extra_instructions()`)

`get_extra_instructions(question)` injects topic-specific instructions into the QA user prompt based on keyword detection. These cost zero tokens when not triggered. Current patterns:

| Pattern | Trigger Keywords | Injected Guidance |
|---|---|---|
| **How-to / process** | "how to", "apply", "deadline", "steps" | Adds "Next Steps" section |
| **Comparison** | "compare", "versus", "vs" | Structures answer as school comparison |
| **Financial aid** | "financial aid", "tuition", "scholarship", "net price", "afford" | Distinguishes sticker vs net price, need-based vs merit aid; uses residency to specify in-state vs out-of-state tuition |
| **Demonstrated interest** | "demonstrated interest", "campus tour", "info session" | Notes DI policies vary; highly selective schools often don't track it |
| **ED/EA/RD strategy** | "early decision", "early action", "when should i apply" | Explains binding/non-binding tradeoffs, financial implications of ED |
| **Rec letters** | "recommendation", "rec letter" | Adds Next Steps for who to ask and timing |
| **FAFSA/CSS** | "fafsa deadline", "css profile", "priority deadline" | FAFSA timing, priority deadlines, CSS Profile requirements |

### `essay_prompt` Parameter

The `essay_prompt` field from `/ask/stream` provides the essay assignment prompt. It gives the model context about what the student is writing. Required by the frontend in essay mode before sending any message.

## Step 6: Post-processing

**Citation verification:** Strips `[N]` where N > source count. Appends warning if no valid citations remain despite sources available.

**Frontend citation rendering:** `[N]` markers are processed by `markdown.tsx` utilities. When sources are hidden (default), `stripCitations()` removes all markers. When the user toggles "Show Sources", `processCitations()` converts them to interactive gray badge elements (`source-badge` class) rendered via `rehype-raw`. Hovering a badge highlights the parent paragraph with a dotted green underline; clicking scrolls to the corresponding SourceCard.

**Confidence scoring:** Based on rerank scores (preferred) or RRF distance scores:
- High: ≥4 hits, avg rerank score > 0.5 (or avg RRF score > 0.6)
- Medium: ≥2 hits, avg rerank score > 0.2 (or avg RRF score > 0.4)
- Low: otherwise

**Reranking status:** Response includes `"reranked": true/false` indicating whether Cohere cross-encoder was used or results are in raw retrieval order.

## Streaming (`answer_question_stream`)

`answer_question_stream()` is a generator method on `CollegeRAG` that yields dicts. It runs the identical pipeline as `answer_question()` (route → rewrite → retrieve → rerank) but streams generation via `openai.ChatCompletion.create(stream=True)`.

**Yield sequence:**
1. Token events: `{"type": "token", "content": "..."}` for each chunk from OpenAI streaming
2. After all tokens: post-processes the assembled answer (citation verification). If verification changed the answer (stripped invalid citations or appended a warning), yields `{"type": "answer_replaced", "content": "..."}` so the frontend can replace the streamed text
3. Metadata: `{"type": "sources", "sources": [...], "confidence": "...", "query_type": "...", "reranked": true/false}`
4. Final: `{"type": "done"}`
5. On exception: `{"type": "error", "message": "..."}`

The `_build_messages()` helper constructs the full message list for all query types, handling history injection, experience context, and prompt selection in one place. This is used only by the streaming path; the sync path uses separate `_generate_qa`, `_generate_essay_ideas`, and `_generate_essay_review` methods.

## Milvus Schema (collection `colleges`)

| Field | Type | Notes |
|---|---|---|
| `id` | VARCHAR(100) | PK |
| `college_name` | VARCHAR(256) | INVERTED index |
| `url` | VARCHAR(2048) | |
| `url_canonical` | VARCHAR(512) | INVERTED index |
| `title` | VARCHAR(500) | |
| `content` | VARCHAR(65535) | `enable_analyzer=True`, english analyzer |
| `content_sparse` | SPARSE_FLOAT_VECTOR | Auto-generated by BM25 function |
| `embedding` | FLOAT_VECTOR(1536) | AUTOINDEX, COSINE metric |
| `page_type` | VARCHAR(64) | INVERTED index. Values: transfer, international, diversity, admissions, academics, financial_aid, outcomes, safety_health, about, campus_life, research, other |
| `crawled_at` | VARCHAR(32) | |

## Chunking

- **Sentence-aware splitting** (default, `CHUNK_SENTENCE_AWARE=1`): Groups sentences into chunks respecting the token limit. No sentence is split mid-text. Falls back to token-based splitting for individual sentences that exceed the limit. Overlap: 1 sentence carried over between chunks.
- **Token-based splitting** (fallback, `CHUNK_SENTENCE_AWARE=0`): Sliding window at exact token boundaries with configurable overlap.
- **Max tokens:** 512 (configurable via `CHUNK_MAX_TOKENS`)
- **Overlap (token mode):** 50 tokens (configurable via `CHUNK_OVERLAP_TOKENS`)
- **Tokenizer:** tiktoken (cl100k_base, matching text-embedding-3-small)
- **Contextual prefixes:** Optional (set `CONTEXTUAL_PREFIXES=1`). Prepends a 2-3 sentence LLM-generated context description to each chunk before embedding, improving retrieval accuracy ~35% (Anthropic benchmarks). Disabled by default — adds ~200ms + ~$0.0003 per chunk during crawl.

Note: Changing the chunking strategy only affects newly crawled pages. Existing vectors retain their original chunking until a full re-crawl with `--no-resume`.

## Page Type Classification

Pages are classified by URL pattern at crawl time into 12 categories. Patterns are defined in `config.py:PAGE_TYPE_PATTERNS` with order-sensitive matching — more specific types (`transfer`, `international`) are matched first to avoid being subsumed by broader categories (`admissions`, `academics`).

Categories: `transfer`, `international`, `diversity`, `admissions`, `academics`, `financial_aid`, `outcomes`, `safety_health`, `about`, `campus_life`, `research`, `other`.

Used by essay mode to target `about`/`academics`/`campus_life`/`diversity`/`outcomes` pages for school personality content.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_SIMPLE` | `gpt-4.1-nano` | Model for simple factual Q&A and greetings |
| `MODEL_STANDARD` | `gpt-5.4-mini` | Model for complex Q&A, essays, predictions |
| `OPENAI_CHAT_MODEL` | — | Legacy override for `MODEL_STANDARD` |
| `COHERE_API_KEY` | — | Cross-encoder reranking (optional) |
| `ZILLIZ_COLLECTION_NAME` | `colleges` | Hybrid search collection |
| `RAG_MAX_CHUNKS_PER_URL` | `2` | URL diversity cap |
| `RETRIEVAL_NPROBE` | `64` | Dense search index probe count (higher = better recall, slightly slower) |
| `RAG_HISTORY_LIMIT` | `6` | Max conversation messages included in generation prompt |
| `RAG_HISTORY_REWRITE_LIMIT` | `3` | Max conversation messages included in query rewrite prompt |
| `CHUNK_MAX_TOKENS` | `512` | Max tokens per chunk |
| `CHUNK_OVERLAP_TOKENS` | `50` | Token overlap between chunks (token-based mode only) |
| `CHUNK_SENTENCE_AWARE` | `1` | Set to `0` to use token-based chunking instead of sentence-aware |
| `CONTEXTUAL_PREFIXES` | `0` | Set to `1` for LLM contextual prefixes |
