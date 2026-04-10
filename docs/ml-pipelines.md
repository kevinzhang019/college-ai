# ML Pipeline Architecture

## Pipeline A: Single Global Model (`train.py`)

One LightGBM binary classifier trained on all data. 36 static features (33 numeric + 3 categorical), plus 2 target encodings added dynamically at training time:

- **Applicant raw (2):** `gpa`, `sat_score`
- **School scalars explicitly kept (7):** `identity_acceptance_rate`, `student_size`, `student_faculty_ratio`, `competitiveness_index`, `sat_range`, `holistic_signal`, `is_yield_protector`
- **Applicant × school academic fit (13):** `sat_percentile_at_school`, `sat_percentile_sq`, `sat_zscore_at_school`, `gpa_zscore_at_school`, `gpa_vs_expected`, `academic_composite_z`, `academic_fit`, `sat_excess`, `gpa_excess`, `sat_ratio`, `sat_above_75th`, `sat_below_25th`, `overqualification_index`
- **Applicant × school selectivity interactions (4):** `gpa_x_acceptance`, `selectivity_x_sat`, `gpa_x_competitiveness`, `sat_x_competitiveness`
- **Applicant × residency × ownership (2):** `instate_x_public`, `residency_x_acceptance`
- **Major × selectivity (1):** `stem_competitive_x_acceptance`
- **Test-policy interactions (4):** `is_test_optional`, `test_optional_x_sat_z`, `test_required_x_sat_below_25th`, `test_optional_x_gpa_zscore` — derived from `admissions_test_requirements` (Scorecard codes: 1=required, 2=recommended, 3=neither, 5=test-flexible)
- **Categorical (3):** `residency`, `major`, `major_tier` — LightGBM native categorical handling, integer-encoded via `pd.Categorical` in `preprocess_data()`
- **Target encodings added dynamically at training time (2):** `school_target_encoded` (Bayesian-smoothed per-school admit rate, smoothing=300, group-aware CV via `StratifiedGroupKFold(groups=school_id)` to prevent leakage), `major_target_encoded` (smoothing=100)

Training: StratifiedGroupKFold on `school_id` → Optuna LightGBMTunerCV (binary_logloss) or custom Brier/NSGA-II loop → early stopping → isotonic/sigmoid/Venn-ABERS calibration (chosen by lowest Brier). Monotone constraints on 16 features.

Artifacts: `model/admissions_lgbm.pkl`, `model/model_config.json`

### Feature selection principle

`school_target_encoded` is a per-school smoothed admit rate, so it already captures any signal that is constant per school. A feature only earns its place in the model if it is one of:

1. **Applicant-side** — varies row-to-row regardless of school (`gpa`, `sat_score`, `residency`, `major`).
2. **Applicant × school interaction** — varies per applicant in a way target encoding cannot express (e.g. `sat_zscore_at_school`, `gpa_x_acceptance`).
3. **Policy flag that modulates the admit function** — a school attribute that changes *how* applicant features map to the outcome (e.g. `is_test_optional` modulates how much a high SAT z-score counts, because at test-optional schools the SAT carries less weight in the admit decision).

Anything else — raw `admissions_sat_25`/`admissions_sat_75`/`student_retention_rate`/`outcome_graduation_rate`/`cost_tuition_*`/`student_pct_*`/Niche ordinal grades — is subsumed by the target encoding for a tree model, so it was dropped during the 2026-04 trim. A handful of per-school scalars (`student_size`, `student_faculty_ratio`, `competitiveness_index`, `sat_range`, `holistic_signal`, `is_yield_protector`) are kept pragmatically.

### Column naming

ML code references the prefixed Scorecard column names (`identity_acceptance_rate`, `admissions_sat_25`, `student_size`, `outcome_graduation_rate`, …) directly — there is no prefix→legacy shim. Adding a new School-table feature is a matter of referencing its prefixed name in `data_pipeline.py`'s training SELECT and in `_get_school_features()` inside `predict.py`; the name then flows through `feature_utils.py`, `train.py`, and `bucket_configs.py` unchanged.

## Pipeline B: Bucketed Models (`train_bucketed.py`)

Four separate LightGBM models, one per selectivity bucket:

| Bucket | Acceptance Rate | Boosting | Calibration | Key Technique |
|---|---|---|---|---|
| **reach** | < 15% | DART | Isotonic | Focal loss (γ=2.0, α=0.25), interaction constraints |
| **competitive** | 15–40% | DART | Isotonic | Interaction constraints, sample weights |
| **match** | 40–70% | GBDT | Venn-ABERS | Linear trees in leaves, pos_bagging |
| **safety** | ≥ 70% | GBDT | Venn-ABERS | Linear trees, heavy pos_bagging (0.5) |

Each bucket has its own target encodings, hyperparams (in `bucket_configs.py`), and calibrator. `BucketedAdmissionsPredictor` routes to the correct bucket model based on school's acceptance rate, falls back to global model if bucketed artifacts missing.

### Bucket Details

**Reach (DART + focal loss):**
- `boosting_type: dart`, `drop_rate: 0.1`, `skip_drop: 0.5`, `max_drop: 50`
- Custom focal loss objective (γ=2.0, α=0.25) — downweights easy examples, focuses on borderline cases
- `FocalLGBWrapper` applies sigmoid to raw log-odds output
- Heavy regularization: `path_smooth=40`, `cat_smooth=30`, `min_sum_hessian_in_leaf=5`
- Interaction constraints: features grouped into `applicant_academic`, `school_stats`, and `fit_interactions` — branches cannot mix groups. The 4 test-policy interactions live inside `fit_interactions`.

**Competitive (DART):**
- Same group interaction constraints as reach
- Less aggressive regularization (`num_leaves=48` vs 31, `max_depth=6` vs 5)
- Sample weights: inverse-sqrt school frequency

**Match (GBDT + linear leaves):**
- `linear_tree: True`, `linear_lambda: 20.0` — ridge regression in leaves
- `pos_bagging_fraction: 0.7`, `neg_bagging_fraction: 1.0` for 5:1 imbalance

**Safety (GBDT + linear leaves):**
- `linear_tree: True`, `linear_lambda: 10.0`
- `pos_bagging_fraction: 0.5` for severe 15:1 imbalance
- `min_sum_hessian_in_leaf: 20`
- Venn-ABERS calibration

### Per-bucket target encodings
School-ID and major target encoded separately per bucket using `StratifiedGroupKFold` smoothing (school smoothing=300, major smoothing=100) to prevent leakage.

### Monotone constraints
16 features constrained (e.g., `gpa +1`, `sat_score +1`, `sat_below_25th -1`, `acceptance_rate +1`).

## Inference Flow

`predict.py` → `get_predictor()` tries bucketed first, falls back to single global. School lookup via `SchoolMatcher` (fuzzy + exact match). Features computed via `feature_utils.compute_features_single()`. `LGBWrapper` wraps raw LightGBM Boosters into sklearn-compatible estimators (inherits `BaseEstimator` for `__sklearn_tags__`/`get_params` required by `CalibratedClassifierCV` and `FrozenEstimator`). Output: calibrated probability, Wilson 95% CI, classification (safety ≥0.6, match ≥0.3, reach <0.3).
