"""
FastAPI server exposing College RAG endpoints.

Run:
  uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 --reload

Or programmatically:
  python -m college_ai.api.app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

import json

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from college_ai.rag.service import CollegeRAG

app = FastAPI(title="College RAG API", version="2.0.0")

# ---- Admissions ML model (lazy-loaded) ----
_admissions_predictor = None


def _get_predictor():
    global _admissions_predictor
    if _admissions_predictor is None:
        try:
            from college_ai.ml.predict import AdmissionsPredictor
            _admissions_predictor = AdmissionsPredictor()
            _admissions_predictor.load()
        except FileNotFoundError:
            return None
    return _admissions_predictor

# CORS: allow env override via comma-separated CORS_ORIGINS, plus localhost defaults
_default_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
_env_origins = os.getenv("CORS_ORIGINS", "")
_origins = [o.strip() for o in _env_origins.split(",") if o.strip()] if _env_origins else []
_all_origins = list(dict.fromkeys(_origins + _default_origins))  # dedupe, env first

app.add_middleware(
    CORSMiddleware,
    allow_origins=_all_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

rag_engine = CollegeRAG()


class AskRequest(BaseModel):
    question: str = Field(..., description="User question or essay request", max_length=2000)
    top_k: int = Field(8, ge=1, le=20)
    college: Optional[str] = Field(
        None, description="Optional college name filter (from dropdown)", max_length=200,
    )
    essay_text: Optional[str] = Field(
        None, description="Pasted essay draft for review mode", max_length=10000,
    )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/config")
def config() -> Dict[str, Any]:
    return {"collection": rag_engine.collection_name}


@app.get("/options")
def get_filter_options() -> Dict[str, Any]:
    """Get available filter options for dropdowns by reading from CSV files."""
    import csv
    from pathlib import Path

    try:
        base_path = Path(__file__).parent.parent / "scraping" / "colleges"
        colleges = set()

        for csv_path in base_path.glob("*.csv"):
            try:
                with open(csv_path, "r", encoding="utf-8") as file:
                    reader = csv.DictReader(file)
                    for row in reader:
                        college_name = row.get("name", "").strip()
                        if college_name:
                            colleges.add(college_name)
            except Exception:
                continue

        # Build school→state mapping from the database
        school_states: Dict[str, str] = {}
        try:
            from college_ai.db.connection import get_session
            from college_ai.db.models import School
            session = get_session()
            try:
                rows = session.query(School.name, School.state).all()
                school_states = {name: state for name, state in rows if state}
            finally:
                session.close()
        except Exception:
            pass  # Degrade gracefully — residency auto-detect won't work

        # Fuzzy-match CSV college names to DB names for state lookup
        # (dropdown names come from CSVs, DB names may differ slightly)
        try:
            from rapidfuzz import fuzz, process as rfprocess
            from college_ai.ml.school_matcher import _normalize

            normalized_db = {_normalize(n): n for n in school_states}
            norm_keys = list(normalized_db.keys())

            for csv_name in colleges:
                if csv_name in school_states:
                    continue
                norm = _normalize(csv_name)
                result = rfprocess.extractOne(
                    norm, norm_keys, scorer=fuzz.token_sort_ratio, score_cutoff=80
                )
                if result:
                    matched_norm, _score, _ = result
                    original_db_name = normalized_db[matched_norm]
                    school_states[csv_name] = school_states[original_db_name]
        except Exception:
            pass  # Degrade gracefully — exact-match still works

        return {"colleges": sorted(colleges), "school_states": school_states}

    except Exception:
        return {
            "colleges": [
                "University of California",
                "Stanford University",
                "MIT",
                "Harvard University",
            ],
            "school_states": {},
        }


@app.post("/ask")
def ask(payload: AskRequest) -> Dict[str, Any]:
    result = rag_engine.answer_question(
        payload.question,
        top_k=payload.top_k,
        college_name=payload.college,
        essay_text=payload.essay_text,
    )
    return result


# ==================== Streaming Endpoint ====================


class HistoryMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'", max_length=20)
    content: str = Field(..., max_length=5000)


class ExperienceItem(BaseModel):
    title: str
    organization: Optional[str] = None
    type: str = Field("", description="extracurricular, project, work, volunteer")
    description: str = ""
    start_date: Optional[str] = Field(None, alias="startDate")
    end_date: Optional[str] = Field(None, alias="endDate")

    class Config:
        populate_by_name = True


class AskStreamRequest(BaseModel):
    question: str = Field(..., description="User question or essay request", max_length=2000)
    top_k: int = Field(8, ge=1, le=20)
    response_length: Optional[str] = Field(None, description="Response length: XS, S, M, L, XL")
    college: Optional[str] = Field(None, max_length=200)
    essay_text: Optional[str] = Field(None, max_length=10000)
    essay_prompt: Optional[str] = Field(None, max_length=1000)
    history: Optional[List[HistoryMessage]] = None
    experiences: Optional[List[ExperienceItem]] = None
    profile: Optional[Dict[str, Any]] = Field(None, description="Student profile: gpa, testScoreType, testScore")


def _sse_generator(payload: AskStreamRequest):
    """Wrap the RAG streaming generator as SSE text lines."""
    history_dicts = None
    if payload.history:
        history_dicts = [{"role": m.role, "content": m.content} for m in payload.history]

    experience_dicts = None
    if payload.experiences:
        experience_dicts = [e.dict() for e in payload.experiences]

    for event in rag_engine.answer_question_stream(
        payload.question,
        top_k=payload.top_k,
        response_length=payload.response_length,
        college_name=payload.college,
        essay_text=payload.essay_text,
        essay_prompt=payload.essay_prompt,
        history=history_dicts,
        experiences=experience_dicts,
        profile=payload.profile,
    ):
        yield f"data: {json.dumps(event)}\n\n"


@app.post("/ask/stream")
def ask_stream(payload: AskStreamRequest):
    return StreamingResponse(
        _sse_generator(payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== Admissions Prediction Endpoints ====================


class PredictRequest(BaseModel):
    gpa: float = Field(..., ge=0, le=5.0, description="Applicant GPA")
    school_name: str = Field(..., description="School name")
    sat: Optional[float] = Field(None, ge=400, le=1600, description="SAT total score")
    act: Optional[float] = Field(None, ge=1, le=36, description="ACT composite score")
    residency: Optional[str] = Field(None, description="'inState' or 'outOfState'")
    major: Optional[str] = Field(None, description="Intended major / field of study")


class CompareRequest(BaseModel):
    gpa: float = Field(..., ge=0, le=5.0)
    sat: Optional[float] = Field(None, ge=400, le=1600)
    act: Optional[float] = Field(None, ge=1, le=36)
    schools: list = Field(..., description="List of school names")
    residency: Optional[str] = None
    major: Optional[str] = None


@app.post("/predict")
def predict_admission(payload: PredictRequest) -> Dict[str, Any]:
    """Predict admission probability for a student at a specific school."""
    predictor = _get_predictor()
    if predictor is None:
        return {"error": "Admissions model not yet trained. Run: python -m college_ai.ml.train"}
    return predictor.predict(
        gpa=payload.gpa,
        school_name=payload.school_name,
        sat=payload.sat,
        act=payload.act,
        residency=payload.residency,
        major=payload.major,
    )


@app.post("/compare")
def compare_schools(payload: CompareRequest) -> Dict[str, Any]:
    """Compare admission probability across multiple schools."""
    predictor = _get_predictor()
    if predictor is None:
        return {"error": "Admissions model not yet trained."}
    results = predictor.compare(
        gpa=payload.gpa,
        sat=payload.sat,
        act=payload.act,
        schools=payload.schools,
        residency=payload.residency,
        major=payload.major,
    )
    return {"results": results}


@app.get("/scattergram/{school_name}")
def get_scattergram(school_name: str) -> Dict[str, Any]:
    """Get scatter plot data for a school's admissions outcomes."""
    try:
        from college_ai.db.connection import get_session
        from college_ai.db.models import ApplicantDatapoint, School
        from college_ai.ml.school_matcher import SchoolMatcher

        matcher = SchoolMatcher()
        school_id = matcher.match(school_name)
        if school_id is None:
            return {"error": f"School '{school_name}' not found."}

        session = get_session()
        try:
            school = session.get(School, school_id)
            datapoints = session.query(ApplicantDatapoint).filter_by(
                school_id=school_id
            ).all()

            return {
                "school": school.name if school else school_name,
                "acceptance_rate": school.acceptance_rate if school else None,
                "sat_range": [school.sat_25, school.sat_75] if school else None,
                "datapoints": [
                    {
                        "gpa": dp.gpa,
                        "sat": dp.sat_score,
                        "act": dp.act_score,
                        "outcome": dp.outcome,
                        "source": dp.source,
                    }
                    for dp in datapoints
                ],
                "total": len(datapoints),
            }
        finally:
            session.close()
    except Exception as e:
        return {"error": str(e)}


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run College RAG API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "college_ai.api.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
