# RAG Pipeline

`rag/service.py` → `CollegeRAG`

## Full Pipeline

1. **Query rewriting** — short queries (<60 chars) expanded via lightweight LLM call
2. **Embedding** — OpenAI `text-embedding-3-small` (1536-dim). Ingestion chunking: tiktoken, 800-token max, 80-token overlap
3. **Vector search** — Milvus `IVF_FLAT` L2, `nprobe=16`. Fetches `top_k × 3` when college filter applied
4. **Post-search filtering:**
   - Hard college filter: exact case-insensitive match on `college_name`
   - Distance threshold: drops hits with L2 > `MAX_L2_DISTANCE=1.2`
   - URL diversity: max `MAX_CHUNKS_PER_URL=2` chunks per URL
5. **Major keyword boosting** — detects major keywords in query, subtracts `0.05 × hit_count` from L2 distance for matching chunks
6. **LLM reranking** — asks LLM to score each chunk 0–10, re-sorts by score
7. **Generation** — OpenAI chat (default `gpt-4.1-nano`) with citation enforcement, source-grounded claims, markdown formatting
8. **ML bridge** (`bridge.py`) — detects admission-probability questions via regex. If GPA + test score + college name found, runs ML predictor and injects result into LLM prompt
9. **Citation verification** — strips/fixes `[N]` refs exceeding source count, warns if no citations
10. **Confidence scoring** — based on hit count + avg L2 distance: high (≥4 hits, <0.6), medium (≥2, <0.9), low otherwise

## Milvus Schema (collection `colleges`)

| Field | Type | Notes |
|---|---|---|
| `id` | VARCHAR(36) | PK, UUID |
| `college_name` | VARCHAR(128) | |
| `url` | VARCHAR(512) | |
| `url_canonical` | VARCHAR(512) | Required, schema enforced |
| `title` | VARCHAR(500) | |
| `content` | VARCHAR(50000) | |
| `embedding` | FLOAT_VECTOR(1536) | |
| `crawled_at` | VARCHAR(32) | |
