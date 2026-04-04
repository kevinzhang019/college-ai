# Scraping Architecture

Three data sources, each with its own scraper module.

## Web Crawler (`crawler.py`)

Multithreaded BFS crawler that embeds college website pages into Zilliz Cloud.

**Architecture:** `ThreadPoolExecutor` with `CRAWLER_MAX_WORKERS=6` threads per college. Multiple colleges crawled in parallel via `INTER_COLLEGE_PARALLELISM=4`.

**Flow:**
1. Seeds loaded from CSV files in `college_ai/scraping/colleges/`
2. Per college: BFS queue seeded with root URL
3. Workers dequeue URLs, fetch pages, extract links, enqueue new URLs
4. Content embedded (OpenAI `text-embedding-3-small`) and batched into Zilliz
5. Stops at `MAX_PAGES_PER_COLLEGE=500`, `MAX_DEPTH=3`, or `MAX_CRAWL_TIME_PER_COLLEGE=300s`

**Anti-bot measures:**
- `curl_cffi` for TLS/JA3 fingerprint impersonation (Chrome/Safari/Edge/Firefox)
- `playwright-stealth` (15+ detection vector patches)
- `camoufox` Firefox-based stealth for deep fingerprint spoofing
- `browserforge` for realistic rotating HTTP headers + fingerprints
- Randomized delays, per-domain adaptive concurrency (token bucket)
- Circuit breaker: `_host_circuit_until` prevents hammering after repeated failures
- Resource blocking: images, stylesheets, fonts, analytics blocked
- Cookie persistence + per-domain YAML profiles

**Delta crawling:** `DeltaCrawlCache` (SQLite, WAL mode) stores ETag, Last-Modified, content hash per URL. Skips unchanged pages on re-crawl.

See [thread-safety.md](thread-safety.md) for concurrency details — this is critical.

## Niche Scraper (`niche_scraper.py`)

Playwright-based scraper for Niche.com scattergram data (GPA/SAT/outcome) and letter grades (12 categories).

**Technology:** Camoufox (Firefox stealth) to bypass Cloudflare/PerimeterX. Requires a free Niche account.

**Threading:** `ThreadPoolExecutor` with `MAX_WORKERS=5`. `DBWriterThread` handles all DB writes via a single queue. See [thread-safety.md](thread-safety.md).

## Scorecard Client (`scorecard_client.py`)

US DOE College Scorecard REST API. Fetches ~6,500 schools' admissions, demographic, and outcomes data. `ThreadPoolExecutor` with `SCORECARD_WORKERS=3`. Upserts into `schools` table.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `ZILLIZ_URI` | required | Zilliz Cloud endpoint |
| `ZILLIZ_API_KEY` | required | Zilliz API key |
| `ZILLIZ_COLLECTION_NAME` | `college_pages` | Milvus collection name |
| `CRAWLER_DELAY` | `1.0` | Inter-request delay (s) |
| `CRAWLER_MAX_WORKERS` | `6` | Threads per college |
| `MAX_PAGES_PER_COLLEGE` | `500` | BFS page cap |
| `MAX_DEPTH` | `3` | BFS depth limit |
| `MAX_CRAWL_TIME_PER_COLLEGE` | `300` | Time budget (s) |
| `INTER_COLLEGE_PARALLELISM` | `4` | Simultaneous colleges |
| `USE_CAMOUFOX` | `1` | Firefox stealth browser |
| `USE_CURL_CFFI` | `1` | TLS fingerprint impersonation |
| `CRAWLER_PROXIES` | empty | Comma-separated proxy list |
| `PLAYWRIGHT_POOL_SIZE` | `5` | Concurrent browsers |
| `ENABLE_DELTA_CRAWLING` | `1` | Skip unchanged pages |
| `MILVUS_INSERT_BUFFER_SIZE` | `50` | Batch insert size |
| `EMBED_MAX_CONCURRENCY` | `3` | Concurrent embed calls |
| `SCORECARD_API_KEY` | required | College Scorecard API key |
| `SCORECARD_WORKERS` | `3` | Scorecard fetch threads |
