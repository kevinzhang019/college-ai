# College Admissions LightGBM Model Training

## Quick Start

### Install Dependencies
```bash
pip install lightgbm optuna scikit-learn shap matplotlib joblib
```

### Run Training (with hyperparameter tuning)
```bash
python -m college_ai.ml.train
```

### Run Training (quick test with defaults)
```bash
python -m college_ai.ml.train --skip-tuning
```

## Features

### Numeric Features (21)
- Student metrics: gpa, sat_score, sat_percentile_at_school, gpa_vs_expected
- School admissions: acceptance_rate, sat_avg, sat_25, sat_75, act_25, act_75
- School size: enrollment, student_faculty_ratio
- Student outcomes: retention_rate, graduation_rate
- Costs: tuition_in_state, tuition_out_of_state, median_earnings_10yr
- Demographics: pct_white, pct_black, pct_hispanic, pct_asian, pct_first_gen

### Categorical Features (5)
- Application metadata: decision_type, applicant_type, source
- Student info: residency
- School info: ownership, selectivity_bucket

## Model Training Pipeline

1. **Data Loading**: Parquet file from `data/training_data.parquet`
2. **Preprocessing**: Missing value imputation, categorical encoding
3. **Splitting**: 80/20 train/test split, stratified by selectivity_bucket
4. **Hyperparameter Tuning**: Optuna optimization over 50 trials, 3-fold CV
5. **Model Training**: LightGBM with best hyperparameters
6. **Calibration**: Platt scaling on held-out calibration set
7. **Evaluation**: AUC-ROC, Brier score, classification metrics
8. **Interpretability**: SHAP summary plot

## Output Files

All artifacts saved to `model/`:
- `admissions_lgbm.pkl`: Trained model + calibrator (joblib)
- `model_config.json`: Feature names and categorical indices
- `shap_summary.png`: SHAP feature importance plot

## Command-Line Options

```
--skip-tuning          Skip hyperparameter tuning, use defaults
--data-path PATH       Path to training data (default: data/training_data.parquet)
--model-dir DIR        Model output directory (default: model)
--n-trials N           Number of Optuna trials (default: 50)
```

## Hyperparameter Search Space

- learning_rate: [0.01, 0.3] (log scale)
- num_leaves: [16, 256]
- max_depth: [3, 12]
- min_child_samples: [5, 100]
- subsample: [0.5, 1.0]
- colsample_bytree: [0.5, 1.0]
- reg_alpha: [1e-8, 10] (log scale)
- reg_lambda: [1e-8, 10] (log scale)

## Using the Trained Model

```python
import joblib
import pandas as pd

# Load model and calibrator
artifacts = joblib.load('model/admissions_lgbm.pkl')
model = artifacts['model']
calibrator = artifacts['calibrator']

# Load feature config
import json
with open('model/model_config.json') as f:
    config = json.load(f)

# Prepare features in same format as training
X = df[config['feature_names']]

# Get predictions
proba = model.predict(X)  # Raw predictions
# Or use calibrator for calibrated probabilities
```
