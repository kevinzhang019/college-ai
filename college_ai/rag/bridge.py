"""
Bridge between the RAG system and the admissions ML model.

Detects admissions-probability questions and injects ML predictions
into the GPT-4o-mini context so the LLM can weave predictions into
its natural language response.
"""

from __future__ import annotations

import re
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Patterns that suggest the user is asking about admission chances
CHANCE_PATTERNS = [
    r"what are my chances",
    r"can i get into",
    r"will i get into",
    r"do i have a chance",
    r"chances of getting in",
    r"admission probability",
    r"how likely am i",
    r"likelihood of acceptance",
    r"chance of admission",
    r"get accepted",
    r"admitted to",
    r"acceptance rate.*gpa|gpa.*acceptance rate",
    r"sat.*chance|chance.*sat",
    r"gpa.*\d+\.\d+.*chance|chance.*gpa.*\d+\.\d+",
]

# Extract GPA and test scores from natural language
GPA_PATTERN = re.compile(r"(?:gpa|grade point average)\s*(?:of|is|:)?\s*(\d+\.?\d*)", re.IGNORECASE)
SAT_PATTERN = re.compile(r"(?:sat|sat score)\s*(?:of|is|:)?\s*(\d{3,4})", re.IGNORECASE)
ACT_PATTERN = re.compile(r"(?:act|act score)\s*(?:of|is|:)?\s*(\d{1,2})", re.IGNORECASE)


def is_admissions_question(question: str) -> bool:
    """Check if a question is asking about admission chances."""
    q_lower = question.lower()
    return any(re.search(p, q_lower) for p in CHANCE_PATTERNS)


def extract_stats(question: str) -> Dict[str, Optional[float]]:
    """Extract GPA, SAT, and ACT from a natural language question."""
    stats: Dict[str, Optional[float]] = {"gpa": None, "sat": None, "act": None}

    gpa_match = GPA_PATTERN.search(question)
    if gpa_match:
        val = float(gpa_match.group(1))
        if 0 < val <= 5.0:
            stats["gpa"] = val

    sat_match = SAT_PATTERN.search(question)
    if sat_match:
        val = float(sat_match.group(1))
        if 400 <= val <= 1600:
            stats["sat"] = val

    act_match = ACT_PATTERN.search(question)
    if act_match:
        val = float(act_match.group(1))
        if 1 <= val <= 36:
            stats["act"] = val

    return stats


def get_prediction_context(
    question: str,
    college_name: Optional[str] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """If the question is about admission chances and contains stats,
    generate a prediction context string to inject into the RAG prompt.

    Stats are first extracted from the question text.  If missing, falls
    back to ``profile`` data (GPA, SAT/ACT from the user's saved profile).

    Returns None if not applicable.
    """
    if not is_admissions_question(question):
        return None

    stats = extract_stats(question)

    # Fall back to profile data when stats aren't in the question text
    if profile:
        if stats["gpa"] is None and profile.get("gpa"):
            stats["gpa"] = float(profile["gpa"])
        if stats["sat"] is None and stats["act"] is None:
            score_type = (profile.get("testScoreType") or "").upper()
            score = profile.get("testScore")
            if score is not None:
                if score_type == "SAT" and 400 <= float(score) <= 1600:
                    stats["sat"] = float(score)
                elif score_type == "ACT" and 1 <= float(score) <= 36:
                    stats["act"] = float(score)

    if stats["gpa"] is None or (stats["sat"] is None and stats["act"] is None):
        return None

    if college_name is None:
        return None

    try:
        from college_ai.ml.predict import get_predictor
        predictor = get_predictor()
        result = predictor.predict(
            gpa=stats["gpa"],
            school_name=college_name,
            sat=stats["sat"],
            act=stats["act"],
        )

        if "error" in result:
            return None

        prob = result["probability"]
        classification = result["classification"]
        ci = result.get("confidence_interval", [0, 0])
        factors = result.get("factors", [])

        lines = [
            f"\n[ML MODEL PREDICTION] Based on our admissions probability model:",
            f"- Estimated admission probability: {prob:.0%} (95% CI: {ci[0]:.0%}-{ci[1]:.0%})",
            f"- Classification: {classification.upper()} school for this student",
            f"- School acceptance rate: {result.get('school_acceptance_rate', 0):.1%}",
        ]

        if factors:
            lines.append("- Key factors:")
            for f in factors:
                impact = "+" if f["impact"] == "positive" else "-"
                lines.append(f"  [{impact}] {f['detail']}")

        lines.append(
            "Note: This prediction is based on self-reported data and should be "
            "treated as a rough estimate, not a guarantee.\n"
            "Guidance for incorporating this prediction:\n"
            "- Frame the probability relative to the school's overall acceptance rate.\n"
            "- Classify the school as: SAFETY (>70%), MATCH (30-70%), or REACH (<30%) "
            "based on the predicted probability.\n"
            "- If REACH, suggest specific actions that could improve candidacy "
            "(strong essays, demonstrated interest if the school tracks it, "
            "ED if financial aid is not a constraint).\n"
            "- Never present the probability as deterministic — admissions involves "
            "holistic review factors not captured by the model."
        )

        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"Prediction failed: {e}")
        return None
