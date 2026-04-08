# RAG Pipeline v2

`rag/service.py` → `CollegeRAG` orchestrator

## Architecture

```
User Query + (optional school) + (optional history) + (optional experiences) + (optional profile w/ location)
    │
    ▼
[Router]  ─── rule-based short-circuits:
    │           essay_text → essay_review | essay_prompt → essay_ideas | greeting → skip
    │           + extract school name(s) from query text (alias/acronym → substring → fuzzy, multi-school, capped at 5)
    ▼
[LLM Classifier]  ─── single gpt-4.1-nano call (classifier.py):
    │   → query_type: qa | essay_ideas | essay_review | admission_prediction | ranking | comparison
    │   → complexity: simple | complex (for model routing)
    │   → categories: school data column prefixes (admissions, student, cost, aid, outcome, institution)
    │   → niche_categories: Niche grade categories for ranking reranking (ranking only)
    ▼
[School Data Fetch ║ Query Rewriting]  ─── run in parallel (ThreadPoolExecutor):
    │   • School data: category-aware fetch (school_data.py)
    │     - Non-ranking/comparison + school detected: fetch_school_data_by_categories()
    │     - Ranking/comparison: batch fetch after retrieval (schools come from RAG results)
    │     - Identity fields + base fields always included
    │   • Query rewriting: gpt-4.1-nano, resolves pronouns/references using
    │     conversation history (last 3 msgs, 400 chars each)
    ▼
[Hybrid Retrieval]  ─── dense (COSINE) + BM25 via Milvus 2.5
    │                     configurable ranker: RRF (default, k=60) or WeightedRanker
    │                     pre-filter by college_name(s) if specified (IN filter for multi-school)
    │                     multi-query: essay (values, culture), admission (stats, reqs)
    │                     retrieval result cache: TTL 5min LRU (256 entries, keyed on query+schools)
    │                     embedding cache: LRU 1024 entries (skips API on repeat queries)
    │                     default candidate pool: 50 (configurable via RAG_RETRIEVAL_TOP_K)
    ▼
[Reranking]  ─── Cohere rerank-v4.0-pro cross-encoder (50 → 8 candidates)
    │              documents include college_name + page_type metadata for better scoring
    │              doc context window: 8000 chars (v4.0's 32K token window)
    │              relevance threshold filter (score ≥ 0.1)
    │              ranking boost: if ranking, boost by niche_rank + niche_categories grades
    │              page type boost: essay modes +0.1 for preferred page types
    │              graceful fallback if COHERE_API_KEY not set
    ▼
[Model Selection]  ─── two-tier routing based on query type + complexity:
    │                    simple Q&A → gpt-4.1-nano (cheap, fast)
    │                    everything else → gpt-5.4-mini (higher quality, 90% cache discount)
    ▼
[Generator]  ─── route to specialized prompt + type instructions:
    │              • QA: grounded answer with citations           (temp 0.2)
    │              • Ranking: QA + RANKING_INSTRUCTIONS           (temp 0.2)
    │              • Comparison: QA + COMPARISON_INSTRUCTIONS     (temp 0.2)
    │              • Essay Ideas: 3-4 brainstorming angles        (temp 0.4)
    │              • Essay Review: coaching feedback on draft      (temp 0.3)
    │              • Admission Prediction: QA + ML bridge         (temp 0.2)
    │              category-aware school data block injected into user prompt (all modes)
    │              [NICHE GRADES] block appended for ranking queries only
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
| `rag/router.py` | Rule-based pre-classifier (greeting/essay short-circuits) + multi-school extraction via `extract_schools()` (alias/acronym dict → substring → rapidfuzz, span-tracking dedup, capped at 5) + greeting detection (skips RAG pipeline) |
| `rag/classifier.py` | Unified LLM query classifier (single gpt-4.1-nano call). Returns `QueryIntent(query_type, complexity, categories, niche_categories)`. Categories are school data column prefixes for selective DB fetching. Niche categories are Niche grade names for ranking reranking (ranking queries only) |
| `rag/school_data.py` | Category-aware school data from Turso DB via `SchoolMatcher` fuzzy matching. `fetch_school_data_by_categories()` for single school, `fetch_school_data_batch()` for multi-school. `format_school_data_block_by_categories()` / `format_multi_school_data_block_by_categories()` for selective `[SCHOOL DATA]` blocks. `format_niche_grades_block()` for ranking-only Niche grades |
| `rag/retrieval.py` | `HybridRetriever` — dense + BM25 hybrid search (configurable RRF or WeightedRanker), school pre-filtering, URL dedup |
| `rag/reranker.py` | Cohere rerank-v4.0-pro wrapper (32K context, 8000 char docs with metadata) with graceful degradation, ranking boost (niche_rank + niche_categories grades + acceptance rate), page type boost (essay modes), exposes `available` property for response metadata |
| `rag/prompts.py` | All system/user prompts: QA, essay ideas, essay review, query rewriting. Type-specific instructions: `RANKING_INSTRUCTIONS`, `COMPARISON_INSTRUCTIONS`. Also `format_experiences()`, `format_profile_context()`, `determine_residency()`, `get_extra_instructions()` (conditional domain knowledge). Multi-turn instructions are inlined into each system prompt for prompt caching. |
| `rag/service.py` | Orchestrator: router → classifier → parallel school data fetch + rewrite → retrieval (with TTL cache) → reranker → generator. Prompt caching via `prompt_cache_key` on all OpenAI calls. |
| `rag/bridge.py` | ML prediction injection for admission-probability questions |
| `rag/embeddings.py` | OpenAI embedding utilities, batch processing, cross-thread batcher, in-memory embedding cache (LRU 1024), sentence-aware chunking, contextual chunk prefix generation (`generate_chunk_context()`) |
| `rag/text_cleaner.py` | HTML cleaning, content extraction, dedup |

## Step 1: Query Routing (`router.py` + `classifier.py`)

Two-stage classification:

**Stage 1 — Rule-based short-circuits (`router.py`, zero latency):**
- **Greeting detection**: short messages (≤8 words) matching greeting patterns ("hi", "hello", "thanks", etc.) → `greeting` type, skips the entire RAG pipeline (including LLM classifier)
- **Essay text provided** → forces `essay_review` type (LLM classifier still runs for categories)
- **Essay prompt provided (no text)** → forces `essay_ideas` type (LLM classifier still runs for categories)
- Everything else → `query_type=None`, defers to LLM classifier

**Stage 2 — Unified LLM classifier (`classifier.py`):**
- Single gpt-4.1-nano call, `max_tokens=80`, `temperature=0`
- Returns `QueryIntent` with four fields:
  - `query_type`: `qa | essay_ideas | essay_review | admission_prediction | ranking | comparison`
  - `complexity`: `simple | complex` (simple only for short single-topic Q&A lookups)
  - `categories`: school data column prefixes (`admissions`, `student`, `cost`, `aid`, `outcome`, `institution`) — controls which DB columns are fetched for `[SCHOOL DATA]` blocks
  - `niche_categories`: Niche grade categories (`academics`, `value`, `food`, `campus`, etc.) — only populated for ranking queries, used for reranker boosting and `[NICHE GRADES]` block
- Falls back to `QueryIntent(query_type="qa", complexity="complex")` on any error
- Router's `query_type` takes precedence when set (essay_text/essay_prompt short-circuits)

**Multi-school extraction** (`extract_schools()` → `List[str]`, capped at 5) — see [multi-school-extraction.md](multi-school-extraction.md) for full details:

All three stages collect **all non-overlapping matches** (not just the best), using character-span tracking to avoid double-counting overlapping text regions. Results are deduplicated by canonical name.

- **Stage 1 — Alias/acronym lookup:** ~100 entries mapping acronyms (MIT, BYU, UCLA, UIUC, etc.), shorthands (UMich, WashU, UPenn, Cal Poly, Ole Miss, etc.), and single-name schools (Harvard, Stanford, etc.) to canonical names. Uses word-boundary regex to avoid false positives (e.g., "bu" inside "about"). Longest alias checked first.
- **Stage 2 — Exact substring match** against known college list (from CSVs), longest match first
- **Stage 3 — Fuzzy ngram matching** via rapidfuzz (`token_sort_ratio`, cutoff 85, ngram length 1-7). Only ngrams that don't overlap already-consumed spans are checked.
- **Dropdown takes absolute precedence:** if the `college` param is set, text extraction is skipped entirely and only the dropdown school is used
- When schools are detected from the prompt, retrieval is filtered to include documents from all of them (single school uses `==` filter, multiple schools use `IN` filter)

## Step 2: Query Rewriting

Always rewrites (no 60-char threshold). Uses gpt-4.1-nano with a prompt optimized for college admissions semantic search. Expands abbreviations (CS, EA, ED, RD, FA, FAFSA).

**History-aware rewriting:** When conversation history is available (streaming path), the last N messages (configurable via `RAG_HISTORY_REWRITE_LIMIT`, default 3) are included in the rewrite prompt so the model can resolve pronouns and implicit references (e.g. "What about their CS program?" after asking about MIT → "MIT Computer Science program admissions requirements"). Each message is truncated to `RAG_HISTORY_REWRITE_CHARS` (default 400) characters to keep rewrite fast.

## Step 3: Hybrid Retrieval (`retrieval.py`)

Uses ORM `Collection.hybrid_search()` with two arms:

| Arm | Field | Metric | Notes |
|---|---|---|---|
| Dense | `embedding` (FLOAT_VECTOR 1536) | COSINE | OpenAI text-embedding-3-small, nprobe configurable via `RETRIEVAL_NPROBE` (default 64) |
| Sparse | `content_sparse` (SPARSE_FLOAT_VECTOR) | BM25 | Auto-generated by Milvus from `content` field |

**Fusion:** Configurable via `RAG_RANKER_TYPE`:
- `rrf` (default): `RRFRanker(k=RAG_RANKER_RRF_K)` — Reciprocal Rank Fusion, ignores raw scores, uses rank position only. k=60 default.
- `weighted`: `WeightedRanker(RAG_DENSE_WEIGHT, RAG_SPARSE_WEIGHT)` — score-based weighted fusion, default 70% dense / 30% sparse. Useful if one arm consistently outperforms the other.

**School filtering:**
- Single school: pre-filter via `expr='college_name == "X"'` on both arms
- Multiple schools: pre-filter via `expr='college_name in ["X", "Y"]'` on both arms
- Fallback threshold scales with school count: need `4 × len(schools)` results from filtered search; if below, fall back to global search + soft score boost (+0.15) for all target schools, sorted descending (higher RRF score = more relevant)
- INVERTED scalar index on `college_name` for millisecond filtering

**Embedding cache:** In-memory LRU cache (1024 entries, thread-safe) on `get_embedding()` keyed by text hash. Eliminates redundant OpenAI API calls for repeated/similar queries.

**Retrieval result cache:** TTL-based LRU cache (256 entries, 5 min TTL, thread-safe) keyed on `(query_text, sorted_school_names)`. Avoids repeated Milvus round-trips for the same query within a short window.

**Contextual chunking:** `generate_chunk_context()` in `embeddings.py` can generate a 1-2 sentence context prefix for each chunk before embedding, per Anthropic's Contextual Retrieval technique (~49% reduction in failed retrievals). The prefix is prepended to the chunk text for embedding but NOT stored in the content field.

**Multi-query retrieval:** Multiple query types use supplemental queries for richer context:

| Query Type | Supplemental Queries | Page Type Boost |
|---|---|---|
| `essay_ideas` / `essay_review` | Per school (capped at 2 schools): `{school} mission values what we look for in students`, `{school} unique programs culture community` | about, academics, campus_life, diversity, outcomes, admissions, safety_health, research (rerank boost +0.1) |
| `admission_prediction` | Per school (capped at 2 schools): `{school} admissions statistics acceptance rate class profile`, `{school} application requirements deadlines` | (none) |

Results are merged and deduped by URL. Page types are never used as hard filters — preferred types receive a rerank score boost instead, so all page types remain retrievable.

**URL diversity:** Max 2 chunks per URL (`MAX_CHUNKS_PER_URL`).

## Step 3.5: Category-Aware School Data Fetch (`school_data.py`)

The LLM classifier determines which school data categories are relevant. `school_data.py` selectively fetches and formats only those columns. The `SchoolMatcher` instance is cached as a lazy module-level singleton to avoid reloading ~6,500 schools on every call.

**Parallelization:** For non-ranking queries, school data fetch runs **in parallel** with query rewriting + embedding + retrieval via `ThreadPoolExecutor(max_workers=1)`. This saves ~50-100ms from the critical path since the DB round-trip is independent of the retrieval path.

**Fetching modes:**

- **Non-ranking/comparison, single school:** `fetch_school_data_by_categories(school, categories)` — fetches only columns for the relevant category prefixes. Identity fields (acceptance rate, URL, etc.) and base fields (name, city, state, ownership) are always included.
- **Non-ranking/comparison, multiple schools:** `fetch_school_data_batch(schools)` — batch-fetches all fields per school, deduplicating by UNITID.
- **Ranking/comparison queries:** `fetch_school_data_batch(candidate_names)` — batch-fetches data for all unique schools in the retrieval results (schools come from RAG, not user input).

**Formatting modes:**

- **All query types with schools:** `format_school_data_block_by_categories(data, categories)` renders a `[SCHOOL DATA]` block with **only the fields for the requested categories**. For multiple schools, `format_multi_school_data_block_by_categories()` concatenates separate `[SCHOOL DATA]` blocks per school.
- **Ranking queries only:** `format_niche_grades_block(school_data_map, hits, niche_categories)` appends a separate `[NICHE GRADES]` block with letter grades per school for the detected Niche categories. This block is clearly labeled: "for internal ranking only, NEVER mention in response."

The LLM is instructed that `[SCHOOL DATA]` statistics can be referenced without citation (verified database). Niche grades must never be mentioned in responses — they only influence ranking order.

**Classification examples:**
- "What is MIT's acceptance rate?" → `categories=["admissions"]`, school data block shows only test score fields
- "How much does Stanford cost?" → `categories=["cost", "aid"]`, shows tuition + aid fields
- "Best schools for food" → ranking, `categories=["cost"]`, `niche_categories=["food"]`, shows cost data + `[NICHE GRADES]` with food grades
- "MIT vs Stanford for CS" → comparison, `categories=["admissions", "outcome"]`, shows admissions + outcomes for both schools

## Step 4: Reranking (`reranker.py`)

Cohere `rerank-v4.0-pro` cross-encoder (32K token context). Takes 50 candidates from retrieval (configurable via `RAG_RETRIEVAL_TOP_K`), returns top `top_k` (default 8, configurable 1–20 via API).

The frontend exposes this as a **context size selector** (XS=3, S=5, M=8, L=12, XL=16) in the input area, allowing users to trade speed for thoroughness.

- Documents include metadata: `"College: {name} | Page: {type}\n{title}\n{content[:8000]}"` — the college_name and page_type help the cross-encoder distinguish sources across schools
- **Relevance threshold:** Hits with rerank_score < 0.1 are filtered out to avoid diluting context with irrelevant passages
- Falls back to retrieval order if `COHERE_API_KEY` not set or API fails
- ~200-400ms latency for 50 documents
- `top_k` parameter controls final count: XS(3), S(5), M(8, default), L(12), XL(16)

### Ranking Boost

For ranking queries (`intent.query_type == "ranking"`), after Cohere reranking but before the relevance threshold filter, `_apply_ranking_boost()` modifies scores using `intent.niche_categories`:

| Boost Component | Weight | Formula | Condition |
|---|---|---|---|
| **Niche rank** | 0.15 | `max(0, 1 - (niche_rank - 1) / 500)` | Skipped if categories == `["other"]` |
| **Acceptance rate** | 0.05 | `1 - acceptance_rate` | Only if `"academics"` in categories |
| **Category grades** | 0.10 | Average of letter-grade-to-numeric across requested categories, normalized to 0-1 | Skipped if categories == `["other"]` |

Grade conversion: A+=4.3, A=4.0, A-=3.7, B+=3.3, B=3.0, B-=2.7, C+=2.3, C=2.0, C-=1.7, D+=1.3, D=1.0, D-=0.7, F=0. Normalized by dividing by 4.3.

Total max boost: ~0.30 on top of Cohere's 0-1 scores. Enough to influence ordering for ranking queries without overriding semantic relevance for non-ranking queries. Hits are re-sorted by boosted score after applying.

School data for the boost is passed via `school_data_map` (dict keyed by lowercased school name). For non-ranking queries, no boost is applied.

### Page Type Boost

For essay modes (`essay_ideas`, `essay_review`), after Cohere reranking (and after any ranking boost), `_apply_page_type_boost()` adds +0.1 to the rerank score of hits whose `page_type` matches the preferred set: `about`, `academics`, `campus_life`, `diversity`, `outcomes`, `admissions`, `safety_health`, `research`. Hits are then re-sorted by boosted score.

This covers most informational page types — only `transfer`, `international`, `financial_aid`, and `other` are non-preferred. The +0.1 boost is enough to break ties and lift preferred types without overriding strong semantic relevance from Cohere.

Not applied to Q&A or admission prediction modes.

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

All prompts use the **Cole** persona ("You are Cole, a college admissions advisor and essay coach"). Six query types with specialized prompts, all enforcing citation grounding:

**QA:** Strict grounding contract — every factual claim needs `[N]` citation. Dynamic length budget based on query type (150-600 words), overridable by `response_length` parameter (XS: 50-100w, S: 100-200w, M: auto-detect, L: 400-600w, XL: 600-900w). Includes a statistics contextualization directive (acceptance rates describe the applicant pool, not individual chances; compare to student's profile when available). Optional "Next Steps" section for actionable queries. Conditional domain knowledge injected via `get_extra_instructions()` (see below). Temperature 0.2.

**Ranking:** Uses QA system prompt + `RANKING_INSTRUCTIONS` injected via `{type_instructions}`. Instructions: numbered list from best to worst, each entry with bold heading + 2-3 sentence justification grounded in `[SCHOOL DATA]` stats. Must respect ordering from Niche grades (via `[NICHE GRADES]` block) without mentioning them. Focus on the student's aspect of interest (e.g. food, academics) as primary driver; other factors as minor supporting details. Direct tone, no hedging, no preamble — start with #1. Temperature 0.2.

**Comparison:** Uses QA system prompt + `COMPARISON_INSTRUCTIONS` injected via `{type_instructions}`. Instructions: structure by dimension (not by school), lead with a quick-glance markdown table of 4-6 key stats, ground every claim in `[SCHOOL DATA]` or sources, highlight meaningful differences (interpret gaps, not just list numbers), balanced treatment (equal depth per school). Focus on the student's aspect of interest as primary dimension. Ends with "## Bottom Line" section: "Choose A if X; choose B if Y." No preamble — start with comparison table. Temperature 0.2.

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

**Non-ranking queries** (one or more schools detected): Category-aware `[SCHOOL DATA]` blocks showing only fields relevant to the question. Available categories:
- **identity:** acceptance rate, aliases, URL, locale
- **admissions:** SAT avg/range, ACT range, test policy
- **student:** enrollment, retention, student-faculty ratio, demographics
- **cost:** tuition (in-state/out-of-state), cost of attendance, net price by income bracket
- **aid:** Pell grant rate, federal loan rate, median debt
- **outcome:** graduation rate, median earnings (10yr)
- **institution:** endowment, faculty salary, instructional spend

When multiple schools are detected, each gets its own `[SCHOOL DATA]` header block. This mirrors the ranking query format so the LLM can distinguish and compare schools side by side.

**Ranking and comparison queries**: Same category-aware `[SCHOOL DATA]` blocks as non-ranking, but for **all schools** in the reranked hits (batch-fetched after retrieval via `fetch_school_data_batch()`). For ranking queries only, a separate `[NICHE GRADES]` block is appended containing letter grades per school for the detected `niche_categories` (e.g. "MIT (#3): Academics A+, Food B+"). This block is clearly labeled "for internal ranking only, NEVER mention in response." Comparison queries do not receive Niche grades.

The LLM is instructed that `[SCHOOL DATA]` statistics can be referenced without `[N]` citations since they come from our verified database. `[NICHE GRADES]` must never be mentioned — they only influence ranking order. Fields that are None are omitted.

### Profile Context Injection

For **all modes** (QA, admission prediction, essay ideas, essay review), `format_profile_context(profile, college_name)` converts the student's academic profile, location, major preferences, and school preferences into a context string inserted into the user prompt as `{profile_context}`. Example: `"Student profile: GPA 3.8, SAT 1450, Residency: in-state, State: CA, Preferred majors (ranked): #1 Computer Science, #2 Data Science, Preferred schools (ranked): #1 MIT, #2 Stanford\nNote: This student is still going through the application process. Their rankings for majors and schools are subject to change."`. This enables the LLM to contextualize statistics against the student's actual credentials, personalize tuition/aid advice based on residency status, tailor program advice to the student's ranked major preferences, understand the student's ranked school preferences, and personalize essay suggestions to the student's academic profile.

**Residency determination** (`determine_residency(profile, college_name)`): When the student has set their country/state and a school is selected, this function fuzzy-matches the school name against the Turso DB via `SchoolMatcher`, retrieves the school's state, and compares:
- Non-US country → `"international"`
- US, same state as school → `"in-state"`
- US, different state → `"out-of-state"`
- Insufficient data → `None` (omitted from prompt)

Profile data flows from the frontend Zustand store (`profile: { gpa, testScoreType, testScore, country, countryLabel, state, preferredMajors, savedSchools }`) → `useStreaming` hook (sent on every request when GPA, country, preferred majors, or saved schools are set) → `/ask/stream` `profile` field → `_build_messages()` → `format_profile_context(profile, college_name)`.

### Conditional Domain Instructions (`get_extra_instructions()`)

`get_extra_instructions(question)` injects topic-specific instructions into the QA user prompt based on keyword detection. These cost zero tokens when not triggered. Current patterns:

| Pattern | Trigger Keywords | Injected Guidance |
|---|---|---|
| **How-to / process** | "how to", "apply", "deadline", "steps" | Adds "Next Steps" section |
| **Financial aid** | "financial aid", "tuition", "scholarship", "net price", "afford" | Distinguishes sticker vs net price, need-based vs merit aid; uses residency to specify in-state vs out-of-state tuition |
| **Demonstrated interest** | "demonstrated interest", "campus tour", "info session" | Notes DI policies vary; highly selective schools often don't track it |
| **ED/EA/RD strategy** | "early decision", "early action", "when should i apply" | Explains binding/non-binding tradeoffs, financial implications of ED |
| **Rec letters** | "recommendation", "rec letter" | Adds Next Steps for who to ask and timing |
| **FAFSA/CSS** | "fafsa deadline", "css profile", "priority deadline" | FAFSA timing, priority deadlines, CSS Profile requirements |

### `essay_prompt` Parameter

The `essay_prompt` field from `/ask/stream` provides the essay assignment prompt. It gives the model context about what the student is writing. Available via the ReviewPanel in the unified chat interface. When `essay_prompt` is provided without `essay_text`, the router auto-classifies as `essay_ideas`. When `essay_text` is also provided, it auto-classifies as `essay_review`. The frontend requires a prompt when essay text is present.

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

Used by essay modes (essay_ideas, essay_review) as a rerank boost signal — hits matching `about`/`academics`/`campus_life`/`diversity`/`outcomes`/`admissions`/`safety_health`/`research` receive a +0.1 rerank score boost. Not used as a hard filter. Q&A and admission prediction modes apply no page type boost.

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
| `RAG_RETRIEVAL_TOP_K` | `50` | Number of candidates retrieved before reranking |
| `RAG_RANKER_TYPE` | `rrf` | Hybrid search merge strategy: `rrf` (Reciprocal Rank Fusion) or `weighted` |
| `RAG_RANKER_RRF_K` | `60` | RRF k parameter (only used when `RAG_RANKER_TYPE=rrf`) |
| `RAG_DENSE_WEIGHT` | `0.7` | Dense arm weight (only used when `RAG_RANKER_TYPE=weighted`) |
| `RAG_SPARSE_WEIGHT` | `0.3` | Sparse/BM25 arm weight (only used when `RAG_RANKER_TYPE=weighted`) |
| `RAG_RERANK_MIN_SCORE` | `0.1` | Minimum rerank score — hits below this are filtered out |
| `RAG_RERANK_DOC_MAX_CHARS` | `8000` | Max chars per document sent to Cohere reranker |
| `RAG_RERANK_NICHE_RANK_WEIGHT` | `0.15` | Ranking boost weight for Niche rank position |
| `RAG_RERANK_ACCEPTANCE_WEIGHT` | `0.05` | Ranking boost weight for acceptance rate (academics only) |
| `RAG_RERANK_GRADE_WEIGHT` | `0.10` | Ranking boost weight for Niche category grades |
| `RAG_RERANK_PAGE_TYPE_BOOST` | `0.1` | Score boost for preferred page types (essay modes) |
| `RAG_SCHOOL_BOOST` | `0.15` | Soft score boost for target schools in fallback global search |
| `RAG_HISTORY_LIMIT` | `6` | Max conversation messages included in generation prompt |
| `RAG_HISTORY_REWRITE_LIMIT` | `3` | Max conversation messages included in query rewrite prompt |
| `RAG_HISTORY_REWRITE_CHARS` | `400` | Max chars per message in query rewrite context |
| `CHUNK_MAX_TOKENS` | `512` | Max tokens per chunk |
| `CHUNK_OVERLAP_TOKENS` | `50` | Token overlap between chunks (token-based mode only) |
| `CHUNK_SENTENCE_AWARE` | `1` | Set to `0` to use token-based chunking instead of sentence-aware |
| `CONTEXTUAL_PREFIXES` | `0` | Set to `1` for LLM contextual prefixes |
