# college-ai-v2

## Retrieval-Augmented Generation (RAG) for College Applications

This repo includes a complete RAG system over your Zilliz/Milvus collection of college website pages. It helps students apply to undergraduate programs by answering questions grounded in crawled content, with a focus on bachelor's degree admissions.

**New**: 🌐 **Web Frontend Available** - A modern, responsive web interface for easy interaction with the RAG system!

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

## Quick Start with Web Frontend

### Option 1: One-Click Startup (Recommended)

Start both the backend API and web frontend with a single command:

```bash
./start.sh
```

This will:

- Start the RAG API server on `http://localhost:8000`
- Start the web frontend on `http://localhost:3000`
- Open your browser to `http://localhost:3000` to use the interface

### Option 2: Manual Startup

#### 1. Start the API server:

```bash
uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 --reload
```

#### 2. Start the frontend (in a new terminal):

```bash
cd frontend
python3 -m http.server 3000
```

#### 3. Open your browser:

Navigate to `http://localhost:3000`

## Alternative Usage Methods

### Start the crawler (optional)

To populate or refresh the collection:

```bash
python college_ai/scraping/crawler.py
```

### RAG via CLI

Ask a question with optional major or college filter:

```bash
python -m college_ai.rag.service --question "How do I apply for Computer Science at MIT?" --major "computer science" --top_k 8
```

### RAG via API

Run the API server:

```bash
uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 --reload
```

Query the API:

```bash
curl -X POST http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "Scholarships for business majors at UCLA?", "major": "business", "top_k": 8}'
```

## Web Frontend Features

The included web frontend (`/frontend/`) provides:

- **Modern UI**: Clean, responsive design that works on desktop and mobile
- **Undergraduate Focus**: Specialized for bachelor's degree programs and freshman admissions
- **Smart Filtering**: Filter by major, college, and number of results
- **Real-time Status**: Connection monitoring and system health indicators
- **Example Questions**: Built-in help with common undergraduate application questions
- **Keyboard Shortcuts**: Enter in any field to submit questions, Shift + Enter for new lines
- **Source Citations**: Direct links to original college pages

For detailed frontend documentation, see [`frontend/README.md`](frontend/README.md).

## API Reference

### Notes

- Retrieval uses the `embedding` vector field and returns `url`, `title`, `content`, `college_name`, `majors`, `crawled_at`.
- **Filtering behavior**: College filter is required when specified; major filter is optional and used for ranking boost.
- Both filters support fuzzy matching (partial text matching).
- Answer generation cites sources as [1], [2], ... mapping to the returned source list.

### CLI Usage
