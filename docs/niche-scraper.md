# Niche Scraper (`niche_scraper.py`)

Playwright-based scraper for Niche.com scattergram data (GPA/SAT/outcome) and letter grades (12 categories).

**Technology:** Camoufox (Firefox stealth) to bypass Cloudflare/PerimeterX. Requires a free Niche account.

**Threading:** `ThreadPoolExecutor` with `MAX_WORKERS=5`. `DBWriterThread` handles all DB writes via a single queue. See [thread-safety-niche.md](thread-safety-niche.md) and [thread-safety-niche-audit.md](thread-safety-niche-audit.md).

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `ZILLIZ_URI` | required | Zilliz Cloud endpoint |
| `ZILLIZ_API_KEY` | required | Zilliz API key |
