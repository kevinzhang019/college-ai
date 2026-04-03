"""
FastAPI server exposing College RAG endpoints.

Run:
  uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 --reload

Or programmatically:
  python -m college_ai.api.app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from college_ai.rag.service import CollegeRAG

app = FastAPI(title="College RAG API", version="0.1.0")

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

# Add CORS middleware to allow frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8080",  # Alternative common dev port
        "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

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


@app.get("/options")
def get_filter_options() -> Dict[str, Any]:
    """Get available filter options for dropdowns by reading from CSV files."""
    import csv
    import os
    from pathlib import Path

    try:
        # Get the path to the colleges directory
        base_path = Path(__file__).parent.parent / "scraping" / "colleges"

        colleges = set()
        majors = set()

        # Define the CSV files and their corresponding major names
        csv_files = {
            "general.csv": "General",
            "business.csv": "Business",
            "computer science.csv": "Computer Science",
        }

        # Read each CSV file to extract college names
        for csv_file, major_name in csv_files.items():
            csv_path = base_path / csv_file
            if csv_path.exists():
                # Add major to list only if it's not "General" (General is default, not a selectable major)
                if major_name != "General":
                    majors.add(major_name)
                try:
                    with open(csv_path, "r", encoding="utf-8") as file:
                        reader = csv.DictReader(file)
                        for row in reader:
                            college_name = row.get("name", "").strip()
                            if college_name:
                                colleges.add(college_name)
                except Exception as csv_error:
                    print(f"Error reading {csv_file}: {csv_error}")
                    continue

        # Convert to sorted lists
        colleges_list = sorted(list(colleges))
        majors_list = sorted(list(majors))

        print(
            f"✅ Loaded {len(colleges_list)} colleges and {len(majors_list)} majors from CSV files"
        )

        return {"colleges": colleges_list, "majors": majors_list}

    except Exception as e:
        print(f"❌ Error reading CSV files: {e}")
        # Fallback with common options
        return {
            "colleges": [
                "University of California",
                "Stanford University",
                "MIT",
                "Harvard University",
            ],
            "majors": ["Computer Science", "Business", "Engineering"],
        }


@app.post("/ask")
def ask(payload: AskRequest) -> Dict[str, Any]:
    result = rag_engine.answer_question(
        payload.question,
        top_k=payload.top_k,
        major=payload.major,
        college_name=payload.college,
    )
    return result


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
