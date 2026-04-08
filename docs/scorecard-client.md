# Scorecard Client (`scorecard_client.py`)

US DOE College Scorecard REST API. Fetches ~6,500 schools across 7 data categories (identity, admissions, student, cost, aid, outcome, institution). `ThreadPoolExecutor` with `SCORECARD_WORKERS=3`. Upserts into `schools` table by UNITID.

## Data Categories

| Category | Fields | Examples |
|---|---|---|
| `identity_` | 6 | acceptance rate, aliases, URL, locale, Carnegie class, religious affiliation |
| `admissions_` | 6 | SAT avg/25th/75th, ACT 25th/75th, test-optional policy |
| `student_` | 12 | enrollment, retention, faculty ratio, demographics, first-gen |
| `cost_` | 10 | tuition, COA, avg net price, net price by 5 income brackets |
| `aid_` | 5 | Pell grant rate, loan rate, median debt, debt percentiles |
| `outcome_` | 2 | graduation rate, median earnings 10yr |
| `institution_` | 4 | endowment, faculty salary, FT faculty rate, instructional spend |

SAT composites (25th/75th) are computed from reading + math section scores. School names are cleaned of campus suffixes (e.g. "- Main Campus").

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `SCORECARD_API_KEY` | required | College Scorecard API key ([register free](https://api.data.gov/signup/)) |
| `SCORECARD_WORKERS` | `3` | Scorecard fetch threads |

## Known Limitations

- **No yield rate** — the Scorecard API does not expose admissions yield. The field was removed from the schema.
- **SAT writing scores** — available but not extracted (College Board discontinued the writing section)
