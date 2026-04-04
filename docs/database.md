# Database

SQLAlchemy ORM with Turso/libSQL in prod, SQLite locally. Three tables defined in `college_ai/db/models.py`.

## Tables

### `schools`
College Scorecard UNITID as primary key.
- **Identification:** `id` (PK = UNITID), `name`, `city`, `state`, `ownership` (1=public, 2=private nonprofit, 3=for-profit)
- **Admissions:** `acceptance_rate`, `sat_avg`, `sat_25`, `sat_75`, `act_25`, `act_75`, `yield_rate`
- **Institution:** `enrollment`, `retention_rate`, `graduation_rate`, `median_earnings_10yr`, `tuition_in_state`, `tuition_out_of_state`, `student_faculty_ratio`
- **Demographics:** `pct_white`, `pct_black`, `pct_hispanic`, `pct_asian`, `pct_first_gen`
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

## Connection (`connection.py`)

- Auth token passed via `connect_args`, NOT URL query string (libSQL driver requirement)
- `with_retry()` pattern detects Hrana WebSocket stream expiry errors (`stream not found`, `stream expired`), resets engine, exponential backoff, up to 3 attempts
- Inline migrations via `_migrate_add_columns()` / `_migrate_drop_columns()` — no migration files

## Environment Variables

| Variable | Purpose |
|---|---|
| `TURSO_DATABASE_URL` | Turso libSQL URL (or local SQLite path) |
| `TURSO_AUTH_TOKEN` | Turso auth token |
| `ADMISSIONS_DB_PATH` | Local SQLite fallback path (default: `data/admissions.db`) |
