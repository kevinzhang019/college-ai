# Scorecard Client (`scorecard_client.py`)

US DOE College Scorecard REST API. Fetches ~6,500 schools' admissions, demographic, and outcomes data. `ThreadPoolExecutor` with `SCORECARD_WORKERS=3`. Upserts into `schools` table.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `SCORECARD_API_KEY` | required | College Scorecard API key |
| `SCORECARD_WORKERS` | `3` | Scorecard fetch threads |
