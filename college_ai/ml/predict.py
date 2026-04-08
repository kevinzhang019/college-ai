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

import lightgbm as lgb
from sklearn.base import BaseEstimator

from college_ai.db.connection import get_session
from college_ai.db.models import School, NicheGrade
from college_ai.ml.concordance import act_to_sat
from college_ai.ml.feature_utils import (
    compute_features_single, selectivity_bucket, major_to_tier,
)

logger = logging.getLogger(__name__)


class LGBWrapper(BaseEstimator):
    """Minimal sklearn-compatible wrapper around a LightGBM Booster.

    Duplicated from train.py so pickle can resolve the class at load time.
    """
    _estimator_type = "classifier"
    classes_ = np.array([0, 1])

    def __init__(self, booster: lgb.Booster):
        self.booster = booster
        self.is_fitted_ = True

    def fit(self, X, y=None):
        return self

    def predict_proba(self, X) -> np.ndarray:
        proba = self.booster.predict(X)
        return np.column_stack([1 - proba, proba])

    def predict(self, X) -> np.ndarray:
        return (self.booster.predict(X) > 0.5).astype(int)

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "model")
MODEL_PATH = os.path.join(MODEL_DIR, "admissions_lgbm.pkl")
CONFIG_PATH = os.path.join(MODEL_DIR, "model_config.json")


class AdmissionsPredictor:
    """Loads trained model and provides admission probability predictions."""

    def __init__(self):
        self.model = None
        self.calibrator = None
        self.category_mappings: Dict[str, Dict[str, int]] = {}
        self.school_avg_admitted_gpa: Dict[int, float] = {}
        self.z_stats: Optional[Dict[str, float]] = None
        self.target_encoding_map: Optional[Dict[int, float]] = None
        self.target_encoding_global_mean: Optional[float] = None
        self.major_encoding_map: Optional[Dict[str, float]] = None
        self.major_encoding_global_mean: Optional[float] = None
        self.config = None
        self._school_cache: Dict[int, Dict] = {}
        self._loaded = False

    def load(self):
        """Load model and config from disk."""
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(
                f"Model not found at {MODEL_PATH}. Run training first: "
                "python -m college_ai.ml.train"
            )

        # Patch __main__ wrappers so pickle can resolve classes
        # (train.py runs as __main__, so classes are saved with that module path)
        import sys
        import __main__
        if not hasattr(__main__, 'LGBWrapper'):
            __main__.LGBWrapper = LGBWrapper
        # CatBoost wrappers also need to be resolvable
        try:
            from college_ai.ml.train import (
                CatBoostWrapper, CatBoostRawPredictor,
            )
            if not hasattr(__main__, 'CatBoostWrapper'):
                __main__.CatBoostWrapper = CatBoostWrapper
            if not hasattr(__main__, 'CatBoostRawPredictor'):
                __main__.CatBoostRawPredictor = CatBoostRawPredictor
        except ImportError:
            pass
        artifacts = joblib.load(MODEL_PATH)
        self.model = artifacts["model"]
        self.calibrator = artifacts.get("calibrator")
        self.category_mappings = artifacts.get("category_mappings", {})
        self.school_avg_admitted_gpa = artifacts.get("school_avg_admitted_gpa", {})
        self.z_stats = artifacts.get("z_stats")
        self.target_encoding_map = artifacts.get("target_encoding_map")
        self.target_encoding_global_mean = artifacts.get("target_encoding_global_mean")
        self.major_encoding_map = artifacts.get("major_encoding_map")
        self.major_encoding_global_mean = artifacts.get("major_encoding_global_mean")

        with open(CONFIG_PATH, "r") as f:
            self.config = json.load(f)

        self._loaded = True
        logger.info("Admissions model loaded.")

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def _get_school_features(self, school_id: int) -> Optional[Dict]:
        """Fetch school features from DB (including niche grades), with caching."""
        if school_id in self._school_cache:
            return self._school_cache[school_id]

        session = get_session()
        try:
            school = session.get(School, school_id)
            if not school:
                return None
            features = {
                "acceptance_rate": school.admissions_rate,
                "sat_avg": school.admissions_sat_avg,
                "sat_25": school.admissions_sat_25,
                "sat_75": school.admissions_sat_75,
                "act_25": school.admissions_act_25,
                "act_75": school.admissions_act_75,
                "enrollment": school.student_size,
                "retention_rate": school.student_retention_rate,
                "graduation_rate": school.outcome_graduation_rate,
                "student_faculty_ratio": school.student_faculty_ratio,
                "ownership": school.ownership,
                "tuition_in_state": school.cost_tuition_in_state,
                "tuition_out_of_state": school.cost_tuition_out_of_state,
                "median_earnings_10yr": school.outcome_median_earnings_10yr,
                "pct_white": school.student_pct_white,
                "pct_black": school.student_pct_black,
                "pct_hispanic": school.student_pct_hispanic,
                "pct_asian": school.student_pct_asian,
                "pct_first_gen": school.student_pct_first_gen,
                "school_name": school.name,
            }
            # Fetch niche grades if available
            ng = session.get(NicheGrade, school_id)
            if ng and not ng.no_data:
                features["niche_grades"] = {
                    "academics": ng.academics,
                    "value": ng.value,
                    "professors": ng.professors,
                    "diversity": ng.diversity,
                    "campus": ng.campus,
                    "overall_grade": ng.overall_grade,
                }
                features["niche_rank"] = ng.niche_rank
                features["avg_annual_cost"] = ng.avg_annual_cost
                features["setting"] = ng.setting
            else:
                features["niche_grades"] = None
                features["niche_rank"] = None
                features["avg_annual_cost"] = None
                features["setting"] = None

            self._school_cache[school_id] = features
            return features
        finally:
            session.close()

    def _find_school_id(self, school_name: str) -> Optional[int]:
        """Find school ID by name (exact or fuzzy)."""
        from college_ai.ml.school_matcher import SchoolMatcher
        matcher = SchoolMatcher()
        return matcher.match(school_name)

    def predict(
        self,
        gpa: float,
        school_name: str,
        sat: Optional[float] = None,
        act: Optional[float] = None,
        residency: Optional[str] = None,
        major: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Predict admission probability.

        Args:
            gpa: Applicant's GPA (0-4.0 scale)
            school_name: Name of the school
            sat: SAT total score (400-1600). Provide sat or act.
            act: ACT composite score (1-36). Converted to SAT if sat not provided.
            residency: 'inState' or 'outOfState' (optional).
            major: Intended major (optional).

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
        has_test_score = 1.0
        if sat is None and act is not None:
            sat = act_to_sat(act)
        elif sat is None and act is None:
            has_test_score = 0.0
            return {"error": "Provide either SAT or ACT score."}

        # Compute engineered features via shared utility
        acc_rate = school_features.get("acceptance_rate") or 0.5
        ownership = school_features.get("ownership")
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
            residency=residency,
            ownership=ownership,
            enrollment=school_features.get("enrollment"),
            median_earnings_10yr=school_features.get("median_earnings_10yr"),
            niche_grades=school_features.get("niche_grades"),
            school_name=school_features.get("school_name"),
            avg_annual_cost=school_features.get("avg_annual_cost"),
            niche_rank=school_features.get("niche_rank"),
            yield_rate=None,
        )

        sat_percentile = engineered["sat_percentile_at_school"]

        # Build feature row — GPA, SAT, school-level features, and engineered features
        row = {
            "gpa": min(gpa, 4.0),
            "sat_score": sat,
            "ownership": ownership,
            "residency": residency,
            "major": major,
            "major_tier": major_to_tier(major),
            "setting": school_features.get("setting"),
            **engineered,
            "has_test_score": has_test_score,
            **{k: v for k, v in school_features.items()
               if k not in ("ownership", "niche_grades", "school_name",
                            "setting", "niche_rank", "avg_annual_cost")},
        }

        # Add target encoding for school if available
        if self.target_encoding_map is not None:
            row["school_target_encoded"] = self.target_encoding_map.get(
                school_id, self.target_encoding_global_mean
            )

        # Add target encoding for major if available
        if self.major_encoding_map is not None and major is not None:
            row["major_target_encoded"] = self.major_encoding_map.get(
                major, self.major_encoding_global_mean
            )
        elif self.major_encoding_map is not None:
            row["major_target_encoded"] = None  # NaN for missing major

        # Create DataFrame with correct column order
        feature_names = self.config["feature_names"]
        df = pd.DataFrame([row])

        # Ensure all expected columns exist
        for col in feature_names:
            if col not in df.columns:
                df[col] = None

        df = df[feature_names]

        # Ensure numeric columns have proper dtype (None values can cause object dtype)
        num_features = self.config.get("numeric_features", [])
        for col in num_features:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Encode categorical columns using saved category mappings
        cat_features = self.config.get("categorical_features", [])
        for col in cat_features:
            if col in df.columns and col in self.category_mappings:
                mapping = self.category_mappings[col]
                val = df[col].iloc[0]
                if val is not None and val in mapping:
                    df[col] = float(mapping[val])
                else:
                    # Unknown or missing category — NaN lets LightGBM use
                    # its learned default split direction
                    df[col] = float("nan")

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
        residency: Optional[str] = None,
        major: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Predict admission probability for multiple schools.

        Returns list of predictions sorted by probability (descending).
        """
        if not schools:
            return []

        results = []
        for school_name in schools:
            result = self.predict(
                gpa=gpa, school_name=school_name, sat=sat, act=act,
                residency=residency, major=major,
            )
            results.append(result)

        # Sort by probability descending
        results.sort(key=lambda x: x.get("probability", 0), reverse=True)
        return results


BUCKETED_DIR = os.path.join(MODEL_DIR, "bucketed")
MANIFEST_PATH = os.path.join(BUCKETED_DIR, "manifest.json")


class BucketedAdmissionsPredictor:
    """Loads per-selectivity-bucket models and routes predictions accordingly.

    Exposes the same interface as AdmissionsPredictor (predict / compare).
    """

    def __init__(self):
        self._bucket_models: Dict[str, Any] = {}
        self._bucket_configs: Dict[str, Any] = {}
        self._bucket_artifacts: Dict[str, Any] = {}
        self.z_stats: Optional[Dict[str, float]] = None
        self.school_avg_admitted_gpa: Dict[int, float] = {}
        self._school_cache: Dict[int, Dict] = {}
        self._loaded = False

    def load(self):
        if not os.path.exists(MANIFEST_PATH):
            raise FileNotFoundError(
                f"Bucketed manifest not found at {MANIFEST_PATH}. "
                "Run: python -m college_ai.ml.train_bucketed"
            )

        with open(MANIFEST_PATH, "r") as f:
            manifest = json.load(f)

        self.z_stats = manifest.get("z_stats")
        raw_gpa = manifest.get("school_avg_admitted_gpa", {})
        self.school_avg_admitted_gpa = {int(k): v for k, v in raw_gpa.items()}

        # Patch __main__.LGBWrapper for pickle resolution
        import __main__
        if not hasattr(__main__, "LGBWrapper"):
            __main__.LGBWrapper = LGBWrapper

        for bucket_name in manifest["buckets"]:
            bucket_dir = os.path.join(BUCKETED_DIR, bucket_name)
            model_path = os.path.join(bucket_dir, "model.pkl")
            config_path = os.path.join(bucket_dir, "config.json")

            if not os.path.exists(model_path):
                logger.warning(f"Missing model for bucket '{bucket_name}', skipping")
                continue

            artifacts = joblib.load(model_path)
            with open(config_path, "r") as f:
                config = json.load(f)

            self._bucket_models[bucket_name] = artifacts.get("model")
            self._bucket_configs[bucket_name] = config
            self._bucket_artifacts[bucket_name] = artifacts

        self._loaded = True
        logger.info(
            f"Bucketed models loaded: {list(self._bucket_models.keys())}"
        )

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def _get_school_features(self, school_id: int) -> Optional[Dict]:
        if school_id in self._school_cache:
            return self._school_cache[school_id]

        session = get_session()
        try:
            school = session.get(School, school_id)
            if not school:
                return None
            features = {
                "acceptance_rate": school.admissions_rate,
                "sat_avg": school.admissions_sat_avg,
                "sat_25": school.admissions_sat_25,
                "sat_75": school.admissions_sat_75,
                "act_25": school.admissions_act_25,
                "act_75": school.admissions_act_75,
                "enrollment": school.student_size,
                "retention_rate": school.student_retention_rate,
                "graduation_rate": school.outcome_graduation_rate,
                "student_faculty_ratio": school.student_faculty_ratio,
                "ownership": school.ownership,
                "tuition_in_state": school.cost_tuition_in_state,
                "tuition_out_of_state": school.cost_tuition_out_of_state,
                "median_earnings_10yr": school.outcome_median_earnings_10yr,
                "pct_white": school.student_pct_white,
                "pct_black": school.student_pct_black,
                "pct_hispanic": school.student_pct_hispanic,
                "pct_asian": school.student_pct_asian,
                "pct_first_gen": school.student_pct_first_gen,
                "school_name": school.name,
            }
            ng = session.get(NicheGrade, school_id)
            if ng and not ng.no_data:
                features["niche_grades"] = {
                    "academics": ng.academics,
                    "value": ng.value,
                    "professors": ng.professors,
                    "diversity": ng.diversity,
                    "campus": ng.campus,
                    "overall_grade": ng.overall_grade,
                }
                features["niche_rank"] = ng.niche_rank
                features["avg_annual_cost"] = ng.avg_annual_cost
                features["setting"] = ng.setting
            else:
                features["niche_grades"] = None
                features["niche_rank"] = None
                features["avg_annual_cost"] = None
                features["setting"] = None

            self._school_cache[school_id] = features
            return features
        finally:
            session.close()

    def _find_school_id(self, school_name: str) -> Optional[int]:
        from college_ai.ml.school_matcher import SchoolMatcher
        matcher = SchoolMatcher()
        return matcher.match(school_name)

    def predict(
        self,
        gpa: float,
        school_name: str,
        sat: Optional[float] = None,
        act: Optional[float] = None,
        residency: Optional[str] = None,
        major: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Predict admission probability using the bucket-specific model."""
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

        # Determine bucket
        acc_rate = school_features.get("acceptance_rate") or 0.5
        bucket = selectivity_bucket(acc_rate)

        if bucket not in self._bucket_models:
            return {
                "error": f"No model for bucket '{bucket}' "
                         f"(acceptance_rate={acc_rate:.2f})"
            }

        artifacts = self._bucket_artifacts[bucket]
        config = self._bucket_configs[bucket]
        calibrator = artifacts.get("calibrator")

        # Compute engineered features
        ownership = school_features.get("ownership")
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
            residency=residency,
            ownership=ownership,
            enrollment=school_features.get("enrollment"),
            median_earnings_10yr=school_features.get("median_earnings_10yr"),
            niche_grades=school_features.get("niche_grades"),
            school_name=school_features.get("school_name"),
            avg_annual_cost=school_features.get("avg_annual_cost"),
            niche_rank=school_features.get("niche_rank"),
            yield_rate=None,
        )

        sat_percentile = engineered["sat_percentile_at_school"]

        # Build feature row
        row = {
            "gpa": min(gpa, 4.0),
            "sat_score": sat,
            "ownership": ownership,
            "residency": residency,
            "major": major,
            "major_tier": major_to_tier(major),
            "setting": school_features.get("setting"),
            **engineered,
            **{k: v for k, v in school_features.items()
               if k not in ("ownership", "niche_grades", "school_name",
                            "setting", "niche_rank", "avg_annual_cost")},
        }

        # Per-bucket target encoding for school
        te_map = artifacts.get("target_encoding_map")
        te_global = artifacts.get("target_encoding_global_mean")
        if te_map is not None:
            row["school_target_encoded"] = te_map.get(school_id, te_global)

        # Per-bucket target encoding for major
        major_map = artifacts.get("major_encoding_map")
        major_global = artifacts.get("major_encoding_global_mean")
        if major_map is not None and major is not None:
            row["major_target_encoded"] = major_map.get(major, major_global)
        elif major_map is not None:
            row["major_target_encoded"] = None

        # Create DataFrame with correct column order (no selectivity_bucket)
        feature_names = config["feature_names"]
        df = pd.DataFrame([row])

        for col in feature_names:
            if col not in df.columns:
                df[col] = None
        df = df[feature_names]

        # Ensure numeric dtype
        num_features = config.get("numeric_features", [])
        for col in num_features:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Encode categoricals
        cat_features = config.get("categorical_features", [])
        cat_mappings = artifacts.get("category_mappings", {})
        for col in cat_features:
            if col in df.columns and col in cat_mappings:
                mapping = cat_mappings[col]
                val = df[col].iloc[0]
                if val is not None and val in mapping:
                    df[col] = float(mapping[val])
                else:
                    df[col] = float("nan")

        # Predict
        if calibrator:
            prob = calibrator.predict_proba(df)[0][1]
        else:
            raw = float(self._bucket_models[bucket].predict(df)[0])
            # Focal loss models output raw log-odds
            if artifacts.get("is_focal_loss", False):
                raw = 1.0 / (1.0 + np.exp(-raw))
            prob = raw

        prob = float(np.clip(prob, 0.01, 0.99))

        # Confidence interval (Wilson score)
        z = 1.96
        n = 100
        center = (prob + z * z / (2 * n)) / (1 + z * z / n)
        margin = z * np.sqrt(
            (prob * (1 - prob) + z * z / (4 * n)) / n
        ) / (1 + z * z / n)
        ci_low = max(0.01, center - margin)
        ci_high = min(0.99, center + margin)

        # Classification
        if prob >= 0.6:
            classification = "safety"
        elif prob >= 0.3:
            classification = "match"
        else:
            classification = "reach"

        # Key factors
        factors = []
        if sat_percentile > 0.75:
            factors.append({
                "factor": "Test scores", "impact": "positive",
                "detail": f"SAT {int(sat)} is above the 75th percentile",
            })
        elif sat_percentile < 0.25:
            factors.append({
                "factor": "Test scores", "impact": "negative",
                "detail": f"SAT {int(sat)} is below the 25th percentile",
            })

        if gpa >= 3.8:
            factors.append({
                "factor": "GPA", "impact": "positive",
                "detail": f"GPA {gpa:.2f} is strong",
            })
        elif gpa < 3.0:
            factors.append({
                "factor": "GPA", "impact": "negative",
                "detail": f"GPA {gpa:.2f} is below typical admits",
            })

        return {
            "probability": round(prob, 4),
            "confidence_interval": [round(ci_low, 4), round(ci_high, 4)],
            "classification": classification,
            "school_name": school_name,
            "school_acceptance_rate": acc_rate,
            "model_bucket": bucket,
            "factors": factors,
        }

    def compare(
        self,
        gpa: float,
        sat: Optional[float] = None,
        act: Optional[float] = None,
        schools: Optional[List[str]] = None,
        residency: Optional[str] = None,
        major: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Predict admission probability for multiple schools."""
        if not schools:
            return []

        results = []
        for school_name in schools:
            result = self.predict(
                gpa=gpa, school_name=school_name, sat=sat, act=act,
                residency=residency, major=major,
            )
            results.append(result)

        results.sort(key=lambda x: x.get("probability", 0), reverse=True)
        return results


# Singleton for API use
_predictor = None


def get_predictor() -> AdmissionsPredictor:
    """Return a predictor instance.

    Tries the bucketed model first (per-selectivity-bucket models); falls back
    to the single global model if bucketed artifacts are not found.
    """
    global _predictor
    if _predictor is not None:
        return _predictor

    # Try bucketed models first
    if os.path.exists(MANIFEST_PATH):
        try:
            bp = BucketedAdmissionsPredictor()
            bp.load()
            _predictor = bp
            logger.info("Using bucketed admissions predictor")
            return _predictor
        except Exception as e:
            logger.warning(f"Failed to load bucketed models: {e}")

    # Fallback to single model
    _predictor = AdmissionsPredictor()
    logger.info("Using single-model admissions predictor (fallback)")
    return _predictor
