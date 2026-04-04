# Thread Safety (CRITICAL)

Thread safety in the crawler and scraper is very important. Both `crawler.py` and `niche_scraper.py` are heavily multithreaded. When modifying these files, pay close attention to the concurrency primitives documented here.

## Crawler (`crawler.py`)

The crawler uses multiple concurrency primitives that must be respected:
- **`self.lock`** — protects `crawled_urls`, `discovered_urls`, stats counters. Always acquire before reading/writing these sets.
- **`self.collection_write_lock`** — exclusive lock for batched Milvus writes
- **`self.collection_query_sema`** — semaphore bounding parallel Milvus queries (default 3)
- **`self.embed_semaphore`** — bounds concurrent embedding API calls (default 3)
- **`self._host_lock`** — protects per-host rate limiting state and adaptive concurrency
- **`self._content_hash_lock`** — protects content dedup cache
- **`self.playwright_semaphore`** — caps concurrent browser instances
- **`self._cookie_storage_lock`**, **`self._pw_profile_cache_lock`** — I/O locks

### Key Patterns

- **`PlaywrightPool`** — thread-local browser instances (`threading.local()`) because Playwright sync API is not thread-safe. Browsers rotate after `PLAYWRIGHT_POOL_ROTATE_AFTER=50` uses for fresh fingerprints. Supports both Chromium and Camoufox.
- **`DeltaCrawlCache`** — thread-local SQLite connections (SQLite connections cannot be shared across threads). WAL journal mode. Stores ETag, Last-Modified, content hash per URL.
- **`MilvusFlushThread`** — dedicated daemon thread drains insert `queue.Queue` in batches (`MILVUS_INSERT_BUFFER_SIZE=50` every 2s), reducing `collection_write_lock` contention ~50x
- **`EmbeddingBatcher`** — consolidates embedding requests from multiple threads into fewer API calls (up to 100 texts per call, max wait 200ms)
- **`ProxyPool`** — lock-protected state with per-proxy semaphores, EMA latency tracking, cooldown on failures, sticky session assignment

## Niche Scraper (`niche_scraper.py`)

- **`DBWriterThread`** — all DB writes go through a single `queue.Queue` to a dedicated writer thread. This eliminates cross-thread Turso WebSocket contention. Never write to the DB from worker threads directly. Includes keepalive SELECT every 60s. Atomic school writes (datapoints + NicheGrade committed together). Retries up to 3x with Hrana error detection and engine reset.
- **`GlobalRateLimiter`** — lock-protected, scales delays by worker count (aggregate rate stays constant regardless of parallelism)
- **`JobClaimer`** — lock-protected dynamic work queue distributing schools across workers
- Worker threads only interact with thread-local Playwright browsers and the shared rate limiter

## General Rules

- Never share Playwright browser instances across threads
- Never share SQLite connections across threads
- Always acquire the appropriate lock before mutating shared state
- Prefer queue-based producer/consumer patterns for cross-thread communication
- When adding new shared state, add a corresponding lock
