"""
FastAPI server exposing College RAG endpoints.

Run:
  uvicorn preference_scraper.utils.rag_api:app --host 0.0.0.0 --port 8000 --reload

Or programmatically:
  python -m preference_scraper.utils.rag_api --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from preference_scraper.utils.rag_service import CollegeRAG


app = FastAPI(title="College RAG API", version="0.1.0")
rag_engine = CollegeRAG()


class AskRequest(BaseModel):
    question: str = Field(..., description="User question")
    top_k: int = Field(8, ge=1, le=20)
    major: Optional[str] = Field(None, description="Optional major focus")
    college: Optional[str] = Field(
        None, description="Optional exact college name filter"
    )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/config")
def config() -> Dict[str, Any]:
    return {"collection": rag_engine.collection_name}


@app.post("/ask")
def ask(payload: AskRequest) -> Dict[str, Any]:
    result = rag_engine.answer_question(
        payload.question,
        top_k=payload.top_k,
        major=payload.major,
        college_name=payload.college,
    )
    return result


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run College RAG API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "preference_scraper.utils.rag_api:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
