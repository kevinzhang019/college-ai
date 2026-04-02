"""
Inference module for the admissions probability model.

Loads the trained LightGBM model and provides prediction functions
for the API layer.
"""

from __future__ import annotations

import os
import json
import logging
from typing import Optional, Dict, Any, List

import numpy as np
import joblib
import pandas as pd

from preference_scraper.admissions.db import get_session
from preference_scraper.admissions.models import School
from preference_scraper.admissions.concordance import act_to_sat
from preference_scraper.admissions.feature_utils import compute_features_single

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(__file__), "model")
MODEL_PATH = os.path.join(MODEL_DIR, "admissions_lgbm.pkl")
CONFIG_PATH = os.path.join(MODEL_DIR, "model_config.json")


class AdmissionsPredictor:
    """Loads trained model and provides admission probability predictions."""

    def __init__(self):
        self.model = None
        self.calibrator = None
        self.label_encoders: Dict[str, Any] = {}
        self.school_avg_admitted_gpa: Dict[int, float] = {}
        self.z_stats: Optional[Dict[str, float]] = None
        self.target_encoding_map: Optional[Dict[int, float]] = None
        self.target_encoding_global_mean: Optional[float] = None
        self.config = None
        self._school_cache: Dict[int, Dict] = {}
        self._loaded = False

    def load(self):
        """Load model and config from disk."""
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(
                f"Model not found at {MODEL_PATH}. Run training first: "
                "python -m preference_scraper.admissions.train"
            )

        artifacts = joblib.load(MODEL_PATH)
        self.model = artifacts["model"]
        self.calibrator = artifacts.get("calibrator")
        self.label_encoders = artifacts.get("label_encoders", {})
        self.school_avg_admitted_gpa = artifacts.get("school_avg_admitted_gpa", {})
        self.z_stats = artifacts.get("z_stats")
        self.target_encoding_map = artifacts.get("target_encoding_map")
        self.target_encoding_global_mean = artifacts.get("target_encoding_global_mean")

        with open(CONFIG_PATH, "r") as f:
            self.config = json.load(f)

        self._loaded = True
        logger.info("Admissions model loaded.")

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def _get_school_features(self, school_id: int) -> Optional[Dict]:
        """Fetch school features from DB, with caching."""
        if school_id in self._school_cache:
            return self._school_cache[school_id]

        session = get_session()
        try:
            school = session.get(School, school_id)
            if not school:
                return None
            features = {
                "acceptance_rate": school.acceptance_rate,
                "sat_avg": school.sat_avg,
                "sat_25": school.sat_25,
                "sat_75": school.sat_75,
                "act_25": school.act_25,
                "act_75": school.act_75,
                "enrollment": school.enrollment,
                "retention_rate": school.retention_rate,
                "graduation_rate": school.graduation_rate,
                "student_faculty_ratio": school.student_faculty_ratio,
                "ownership": school.ownership,
                "tuition_in_state": school.tuition_in_state,
                "tuition_out_of_state": school.tuition_out_of_state,
                "median_earnings_10yr": school.median_earnings_10yr,
                "pct_white": school.pct_white,
                "pct_black": school.pct_black,
                "pct_hispanic": school.pct_hispanic,
                "pct_asian": school.pct_asian,
                "pct_first_gen": school.pct_first_gen,
                "yield_rate": school.yield_rate,
            }
            self._school_cache[school_id] = features
            return features
        finally:
            session.close()

    def _find_school_id(self, school_name: str) -> Optional[int]:
        """Find school ID by name (exact or fuzzy)."""
        from preference_scraper.admissions.school_matcher import SchoolMatcher
        matcher = SchoolMatcher()
        return matcher.match(school_name)

    def predict(
        self,
        gpa: float,
        school_name: str,
        sat: Optional[float] = None,
        act: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Predict admission probability.

        Args:
            gpa: Applicant's GPA (0-4.0 scale)
            school_name: Name of the school
            sat: SAT total score (400-1600). Provide sat or act.
            act: ACT composite score (1-36). Converted to SAT if sat not provided.

        Returns:
            Dict with probability, confidence_interval, classification, and factors.
        """
        self._ensure_loaded()

        # Resolve school
        school_id = self._find_school_id(school_name)
        if school_id is None:
            return {"error": f"School '{school_name}' not found in database."}

        school_features = self._get_school_features(school_id)
        if school_features is None:
            return {"error": f"No data for school ID {school_id}."}

        # Normalize test score
        if sat is None and act is not None:
            sat = act_to_sat(act)
        elif sat is None and act is None:
            return {"error": "Provide either SAT or ACT score."}

        # Compute engineered features via shared utility
        acc_rate = school_features.get("acceptance_rate") or 0.5
        engineered = compute_features_single(
            gpa=min(gpa, 4.0),
            sat=sat,
            acceptance_rate=school_features.get("acceptance_rate"),
            sat_avg=school_features.get("sat_avg"),
            sat_25=school_features.get("sat_25"),
            sat_75=school_features.get("sat_75"),
            graduation_rate=school_features.get("graduation_rate"),
            avg_admitted_gpa=self.school_avg_admitted_gpa.get(school_id),
            z_stats=self.z_stats,
        )

        sat_percentile = engineered["sat_percentile_at_school"]

        # Build feature row — GPA, SAT, school-level features, and engineered features
        row = {
            "gpa": min(gpa, 4.0),
            "sat_score": sat,
            "ownership": school_features.get("ownership"),
            **engineered,
            **{k: v for k, v in school_features.items()
               if k not in ("ownership",)},
        }

        # Add target encoding for school if available
        if self.target_encoding_map is not None:
            row["school_target_encoded"] = self.target_encoding_map.get(
                school_id, self.target_encoding_global_mean
            )

        # Create DataFrame with correct column order
        feature_names = self.config["feature_names"]
        df = pd.DataFrame([row])

        # Ensure all expected columns exist
        for col in feature_names:
            if col not in df.columns:
                df[col] = None

        df = df[feature_names]

        # Encode categorical columns using the same label encoders from training
        cat_features = self.config.get("categorical_features", [])
        for col in cat_features:
            if col in df.columns and col in self.label_encoders:
                le = self.label_encoders[col]
                val = str(df[col].iloc[0])
                if val in le.classes_:
                    df[col] = le.transform(df[col].astype(str))
                else:
                    # Unseen category — map to the 'unknown' class if it exists,
                    # otherwise use 0 as a safe fallback
                    if 'unknown' in le.classes_:
                        df[col] = le.transform(['unknown'] * len(df))
                    else:
                        df[col] = 0

        # Predict
        if self.calibrator:
            prob = self.calibrator.predict_proba(df)[0][1]
        else:
            # lgb.Booster.predict() returns probabilities directly
            prob = float(self.model.predict(df)[0])

        prob = float(np.clip(prob, 0.01, 0.99))

        # Confidence interval (approximate using Wilson score interval)
        z = 1.96  # 95% CI
        n = 100  # pseudo sample size
        center = (prob + z * z / (2 * n)) / (1 + z * z / n)
        margin = z * np.sqrt((prob * (1 - prob) + z * z / (4 * n)) / n) / (1 + z * z / n)
        ci_low = max(0.01, center - margin)
        ci_high = min(0.99, center + margin)

        # Classification
        if prob >= 0.6:
            classification = "safety"
        elif prob >= 0.3:
            classification = "match"
        else:
            classification = "reach"

        # Key factors (simplified — full SHAP requires more compute)
        factors = []
        if sat_percentile > 0.75:
            factors.append({"factor": "Test scores", "impact": "positive",
                           "detail": f"SAT {int(sat)} is above the 75th percentile"})
        elif sat_percentile < 0.25:
            factors.append({"factor": "Test scores", "impact": "negative",
                           "detail": f"SAT {int(sat)} is below the 25th percentile"})

        if gpa >= 3.8:
            factors.append({"factor": "GPA", "impact": "positive",
                           "detail": f"GPA {gpa:.2f} is strong"})
        elif gpa < 3.0:
            factors.append({"factor": "GPA", "impact": "negative",
                           "detail": f"GPA {gpa:.2f} is below typical admits"})

        return {
            "probability": round(prob, 4),
            "confidence_interval": [round(ci_low, 4), round(ci_high, 4)],
            "classification": classification,
            "school_name": school_name,
            "school_acceptance_rate": acc_rate,
            "factors": factors,
        }

    def compare(
        self,
        gpa: float,
        sat: Optional[float] = None,
        act: Optional[float] = None,
        schools: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Predict admission probability for multiple schools.

        Returns list of predictions sorted by probability (descending).
        """
        if not schools:
            return []

        results = []
        for school_name in schools:
            result = self.predict(gpa=gpa, school_name=school_name, sat=sat, act=act)
            results.append(result)

        # Sort by probability descending
        results.sort(key=lambda x: x.get("probability", 0), reverse=True)
        return results


# Singleton for API use
_predictor = None


def get_predictor() -> AdmissionsPredictor:
    global _predictor
    if _predictor is None:
        _predictor = AdmissionsPredictor()
    return _predictor
