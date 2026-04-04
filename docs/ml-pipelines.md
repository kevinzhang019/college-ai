# ML Pipeline Architecture

## Pipeline A: Single Global Model (`train.py`)

One LightGBM binary classifier trained on all data. 84 features total:
- **Raw numeric (24):** GPA, SAT, acceptance_rate, SAT percentiles, enrollment, retention, graduation, demographics, earnings, yield
- **Engineered (34):** z-scores, percentiles, interactions (gpa×acceptance, selectivity×sat), overqualification features, log transforms, Niche ordinal grades, cost ratios, academic fit signals
- **Categorical (6):** ownership, selectivity_bucket, residency, major, setting, major_tier
- **Target encodings (2):** school_target_encoded, major_target_encoded

Training: StratifiedGroupKFold on school_id → Optuna LightGBMTunerCV (binary_logloss) or custom Brier/NSGA-II loop → early stopping → isotonic/sigmoid/Venn-ABERS calibration (chosen by lowest Brier). Monotone constraints on 16 features.

Artifacts: `model/admissions_lgbm.pkl`, `model/model_config.json`

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
- Interaction constraints: features grouped into `applicant_academic`, `school_stats`, `fit_interactions`, `demographics` — branches cannot mix groups

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

`predict.py` → `get_predictor()` tries bucketed first, falls back to single global. School lookup via `SchoolMatcher` (fuzzy + exact match). Features computed via `feature_utils.compute_features_single()`. Output: calibrated probability, Wilson 95% CI, classification (safety ≥0.6, match ≥0.3, reach <0.3).
