# college-ai-v2

## Retrieval-Augmented Generation (RAG) for College Applications

This repo now includes a RAG system over your Zilliz/Milvus collection of college website pages. It helps students apply to college by answering questions grounded in crawled content.

### Prerequisites

- Environment variables in `.env` at the project root:
  - `ZILLIZ_URI` and `ZILLIZ_API_KEY`
  - `ZILLIZ_COLLECTION_NAME` (defaults to `college_pages`)
  - `OPENAI_API_KEY`
  - Optional: `OPENAI_CHAT_MODEL` (default `gpt-4o-mini`)

Install dependencies:

```bash
pip install -r requirements.txt
```

### Start the crawler (optional)

To populate or refresh the collection:

```bash
python preference_scraper/crawlers/multithreaded_crawler.py
```

### RAG via CLI

Ask a question with optional major or college filter:

```bash
python -m preference_scraper.utils.rag_service --question "How do I apply for Computer Science at MIT?" --major "computer science" --top_k 8
```

### RAG via API

Run the API server:

```bash
uvicorn preference_scraper.utils.rag_api:app --host 0.0.0.0 --port 8000 --reload
```

Query the API:

```bash
curl -X POST http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Scholarships for business majors at UCLA?", "major": "business", "top_k": 8}'
```

### Notes

- Retrieval uses the `embedding` vector field and returns `url`, `title`, `content`, `college_name`, `majors`, `crawled_at`.
- You can filter by `college_name` (exact match) and prioritize results by `major`.
- Answer generation cites sources as [1], [2], ... mapping to the returned source list.
