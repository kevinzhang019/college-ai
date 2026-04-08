# Database

SQLAlchemy ORM with Turso/libSQL in prod, SQLite locally. Three tables defined in `college_ai/db/models.py`.

## Tables

### `schools`
College Scorecard UNITID as primary key. 51 columns organized by category prefix for programmatic grouping. All rates/percentages stored as 0.0–1.0, all dollar amounts in nominal USD.

- **Core:** `id` (PK = UNITID), `name`, `city`, `state`, `ownership` (1=public, 2=private nonprofit, 3=for-profit)
- **`identity_`** (6) — acceptance rate, aliases, URL, locale code, Carnegie classification, religious affiliation
- **`admissions_`** (6) — SAT avg/25th/75th, ACT 25th/75th, test-optional policy (1=required, 2=recommended, 3=neither, 5=flexible)
- **`student_`** (12) — enrollment size, retention rate, faculty ratio, avg entry age, gender split, part-time share, race/ethnicity breakdown, first-gen share
- **`cost_`** (10) — in/out-of-state tuition, total COA, avg net price, booksupply, net price by 5 income brackets ($0–30k, $30–48k, $48–75k, $75–110k, $110k+)
- **`aid_`** (5) — Pell grant rate, federal loan rate, median debt at graduation, debt 25th/75th percentile
- **`outcome_`** (2) — graduation rate (IPEDS 150% time), median earnings 10yr post-entry
- **`institution_`** (4) — endowment, avg monthly faculty salary, full-time faculty rate, instructional spend per FTE
- `updated_at`

### `applicant_datapoints`
Individual Niche scattergram data points.
- `id` (autoincrement PK), `school_id` (FK → schools), `source` (='niche')
- `gpa`, `sat_score`, `act_score`
- `outcome`: 'accepted' | 'rejected' | 'waitlisted'
- `residency`: 'inState' | 'outOfState'
- `major`: intended major string
- `scraped_at`

### `niche_grades`
Niche.com letter grades per school.
- `school_id` (PK + FK → schools)
- `overall_grade`, `niche_rank`
- 12 category grades: `academics`, `value`, `diversity`, `campus`, `athletics`, `party_scene`, `professors`, `location`, `dorms`, `food`, `student_life`, `safety`
- Quantitative: `acceptance_rate_niche`, `avg_annual_cost`, `graduation_rate_niche`, `student_faculty_ratio_niche`, `setting`, `religious_affiliation`, `pct_students_on_campus`, `pct_greek_life`, `avg_rating`, `review_count`
- `no_data` flag, `updated_at`

**RAG usage:** Both `schools` and `niche_grades` data is fetched by `rag/school_data.py` when a school is detected. The data is injected into LLM prompts as a structured `[SCHOOL DATA]` block and used by the reranker for ranking-query score boosting (niche_rank, category grades, acceptance_rate).

## Connection (`connection.py`)

- Auth token passed via `connect_args`, NOT URL query string (libSQL driver requirement)
- `with_retry()` pattern detects Hrana WebSocket stream expiry errors (`stream not found`, `stream expired`), resets engine, exponential backoff, up to 3 attempts. Turso plan-level blocks (quota exhaustion) are detected separately by `is_blocked_error()` and fail fast without retries — these are not transient.
- Inline migrations via `_migrate_add_columns()` / `_migrate_drop_columns()` — no migration files. Supports `ALTER TABLE RENAME COLUMN` (libSQL/SQLite 3.25+) for schema refactors

## Environment Variables

| Variable | Purpose |
|---|---|
| `TURSO_DATABASE_URL` | Turso libSQL URL (or local SQLite path) |
| `TURSO_AUTH_TOKEN` | Turso auth token |
| `ADMISSIONS_DB_PATH` | Local SQLite fallback path (default: `data/admissions.db`) |
