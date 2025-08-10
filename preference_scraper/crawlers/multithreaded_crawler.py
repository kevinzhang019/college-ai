"""
Multithreaded College Site Crawler
Reads college URLs from CSV files and performs multithreaded crawling of each site.
Uses BeautifulSoup to find internal links and uploads each page directly to Milvus.
"""

import os
import sys
import csv
import glob
import time
import uuid
import threading
import queue
import random
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Dict, List, Any, Optional, Set
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode
import hashlib
import requests
import re
import yaml

try:
    from curl_cffi import requests as curl_requests  # type: ignore
except Exception:
    curl_requests = None
# Optional Playwright (sync API) for JS-rendered fallback
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None  # type: ignore
    PlaywrightTimeoutError = Exception  # type: ignore
from bs4 import BeautifulSoup
from pymilvus import (
    connections,
    Collection,
    FieldSchema,
    CollectionSchema,
    DataType,
    utility,
)


# ==================== Proxy Pool ====================
class ProxyPool:
    """Thread-safe, health-aware proxy pool with bounded concurrency per proxy and sticky assignments."""

    def __init__(
        self,
        proxies: List[str],
        max_concurrency_per_proxy: int = 2,
        cooldown_sec: int = 120,
        max_consec_fails: int = 3,
        sticky_requests: int = 3,
    ) -> None:
        self.proxies = list(dict.fromkeys([p.strip() for p in proxies if p.strip()]))
        self.max_concurrency_per_proxy = max(1, int(max_concurrency_per_proxy))
        self.cooldown_sec = max(10, int(cooldown_sec))
        self.max_consec_fails = max(1, int(max_consec_fails))
        self.sticky_requests = max(1, int(sticky_requests))
        self._lock = threading.Lock()
        # Per-proxy metrics/state
        self._state: Dict[str, Dict[str, Any]] = {}
        for proxy in self.proxies:
            self._state[proxy] = {
                "sema": threading.Semaphore(self.max_concurrency_per_proxy),
                "successes": 0,
                "failures": 0,
                "consec_fails": 0,
                "avg_latency_ms": None,
                "cooldown_until": 0.0,
                "last_status": None,
            }
        # Sticky assignments: key -> {proxy, remaining}
        self._sticky: Dict[tuple, Dict[str, Any]] = {}

    def _is_available(self, proxy: str, now_ts: float) -> bool:
        st = self._state[proxy]
        if now_ts < st["cooldown_until"]:
            return False
        # Try non-blocking acquire to test capacity
        acquired = st["sema"].acquire(blocking=False)
        if not acquired:
            return False
        # Put it back; actual acquire happens in acquire()
        st["sema"].release()
        return True

    def _score(self, proxy: str) -> float:
        st = self._state[proxy]
        succ = st["successes"]
        fail = st["failures"]
        total = succ + fail
        success_rate = (succ / total) if total > 0 else 0.5
        latency = st["avg_latency_ms"] if st["avg_latency_ms"] is not None else 500.0
        # Higher is better: prioritize higher success rate and lower latency
        return success_rate * 1.0 - (latency / 5000.0)

    def acquire(self, netloc: str, sticky_key: Optional[tuple] = None) -> tuple:
        """Return (proxy_url or None, token) where token must be passed to release()."""
        if not self.proxies:
            return None, None
        now_ts = time.time()
        with self._lock:
            # Try sticky
            if sticky_key and sticky_key in self._sticky:
                entry = self._sticky[sticky_key]
                proxy = entry.get("proxy")
                remaining = int(entry.get("remaining", 0))
                if (
                    proxy in self._state
                    and remaining > 0
                    and self._is_available(proxy, now_ts)
                ):
                    # consume one sticky use and actually acquire (non-blocking)
                    if self._state[proxy]["sema"].acquire(blocking=False):
                        entry["remaining"] = remaining - 1
                        return proxy, {
                            "proxy": proxy,
                            "sticky_key": sticky_key,
                            "start": time.monotonic(),
                        }
                else:
                    # drop sticky
                    self._sticky.pop(sticky_key, None)

            # Choose best available proxy by score
            candidates = [p for p in self.proxies if self._is_available(p, now_ts)]
            if not candidates:
                return None, None
            # Sort by score descending
            candidates.sort(key=self._score, reverse=True)
            # Try top 3 candidates (or fewer)
            for proxy in candidates[:3]:
                # Take capacity now (non-blocking)
                if self._state[proxy]["sema"].acquire(blocking=False):
                    if sticky_key:
                        self._sticky[sticky_key] = {
                            "proxy": proxy,
                            "remaining": self.sticky_requests - 1,
                        }
                    return proxy, {
                        "proxy": proxy,
                        "sticky_key": sticky_key,
                        "start": time.monotonic(),
                    }
            return None, None

    def release(
        self,
        token: Optional[Dict[str, Any]],
        success: bool,
        status_code: Optional[int] = None,
        error: Optional[Exception] = None,
        latency_ms: Optional[float] = None,
    ) -> None:
        if not token or "proxy" not in token:
            return
        proxy = token["proxy"]
        if proxy not in self._state:
            return
        with self._lock:
            # prevent double release
            if token.get("released"):
                return
            token["released"] = True
            st = self._state[proxy]
            # Update latency
            if latency_ms is None and token.get("start"):
                latency_ms = max(0.0, (time.monotonic() - token["start"]) * 1000.0)
            if latency_ms is not None:
                if st["avg_latency_ms"] is None:
                    st["avg_latency_ms"] = float(latency_ms)
                else:
                    st["avg_latency_ms"] = 0.8 * st["avg_latency_ms"] + 0.2 * float(
                        latency_ms
                    )
            # Update outcomes
            if success:
                st["successes"] += 1
                st["consec_fails"] = 0
                st["last_status"] = status_code
            else:
                st["failures"] += 1
                st["consec_fails"] += 1
                st["last_status"] = status_code
                # Cooldown on repeated failures or specific statuses
                if st["consec_fails"] >= self.max_consec_fails or (
                    status_code in {403, 429}
                ):
                    st["cooldown_until"] = time.time() + self.cooldown_sec
            # Release capacity
            try:
                st["sema"].release()
            except Exception:
                pass


# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from preference_scraper.utils.openai_embed import (
    get_embedding,
    get_embeddings_batch,
    chunk_text_by_tokens,
)
from preference_scraper.utils.text_cleaner import clean_text
from preference_scraper.crawlers.config import *


class MultithreadedCollegeCrawler:
    """Multithreaded crawler that crawls college websites and uploads directly to Milvus."""

    def __init__(self, delay: float = None, max_workers: int = None):
        """
        Initialize the crawler.

        Args:
            delay: Delay between requests to be respectful (uses config if None)
            max_workers: Number of worker threads per college (uses config if None)
        """
        self.delay = delay or CRAWLER_DELAY
        self.max_workers = max_workers or CRAWLER_MAX_WORKERS
        self.colleges_dir = os.path.join(os.path.dirname(__file__), "colleges")

        # Ensure colleges directory exists
        os.makedirs(self.colleges_dir, exist_ok=True)

        # Initialize session for requests with realistic headers
        self.session = requests.Session()

        # Rotate User-Agents to avoid detection
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/121.0",
        ]

        self.session.headers.update(
            {
                "User-Agent": random.choice(user_agents),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Cache-Control": "max-age=0",
                "Referer": "https://www.google.com/",
            }
        )

        # Anti-bot detection settings
        self.min_delay = max(0.5, self.delay * 0.5)  # Minimum delay
        self.max_delay = self.delay * 2.0  # Maximum delay for randomization
        self.max_retries = MAX_RETRIES

        # User-Agent rotation for anti-detection
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/121.0",
        ]

        # Thread-safe sets for preventing duplicates
        self.crawled_urls = set()
        self.discovered_urls = set()
        self.uploaded_urls = set()  # Track URLs already uploaded to Milvus
        self.existing_urls = (
            set()
        )  # URLs that already exist in Milvus from previous runs
        self.lock = threading.Lock()
        # Serialize Milvus inserts to avoid driver-side races; keep stats lock separate
        self.insert_lock = threading.Lock()
        # Concurrency controls for Milvus operations
        # - Queries: allow bounded parallelism
        # - Writes: exclusive
        query_parallelism = int(os.getenv("MILVUS_QUERY_PARALLELISM", "3") or "3")
        self.collection_query_sema = threading.Semaphore(max(1, query_parallelism))
        self.collection_write_lock = threading.Lock()
        # Limit concurrent embedding generation to reduce rate-limit errors
        try:
            embed_concurrency = int(os.getenv("EMBED_MAX_CONCURRENCY", "2"))
        except Exception:
            embed_concurrency = 2
        self.embed_semaphore = threading.Semaphore(max(1, embed_concurrency))

        # Per-host rate limiting and adaptive concurrency state (thread-safe)
        self._host_lock = threading.Lock()
        self._host_tokens: Dict[str, Dict[str, Any]] = {}
        self._host_concurrency: Dict[str, int] = {}
        self._host_failures: Dict[str, int] = {}
        self._host_circuit_until: Dict[str, float] = {}
        self.max_concurrency_per_host = int(
            os.getenv("MAX_CONCURRENCY_PER_HOST", "6") or "6"
        )
        self.min_concurrency_per_host = 1
        self.token_refill_per_sec = float(
            os.getenv("HOST_TOKEN_REFILL_PER_SEC", "2.5") or "2.5"
        )
        self.max_tokens_per_host = int(os.getenv("HOST_MAX_TOKENS", "6") or "6")

        # Playwright fallback controls
        self.playwright_enabled = os.getenv("USE_PLAYWRIGHT_FALLBACK", "1") == "1"
        try:
            pw_conc = int(os.getenv("PLAYWRIGHT_MAX_CONCURRENCY", "3") or "3")
        except Exception:
            pw_conc = 3
        self.playwright_max_workers = max(1, pw_conc)
        self.playwright_semaphore = threading.Semaphore(self.playwright_max_workers)
        try:
            self.playwright_nav_timeout_ms = int(
                os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "15000") or "15000"
            )
        except Exception:
            self.playwright_nav_timeout_ms = 15000
        # Profiles directory and cache
        self.playwright_profiles_dir = os.path.join(
            os.path.dirname(__file__), "playwright_profiles"
        )
        self._pw_profile_cache_lock = threading.Lock()
        self._pw_profile_cache: Dict[str, Dict[str, Any]] = {}
        # Playwright runtime and browser cache (thread-safe)
        self._pw = None  # lazily started sync_playwright instance
        self._pw_browser_lock = threading.Lock()
        self._pw_browsers: Dict[str, Any] = {}

        # Proxy pool initialization (optional)
        try:
            max_per_proxy = int(
                os.getenv("PROXY_MAX_CONCURRENCY_PER_PROXY", "2") or "2"
            )
            cooldown_sec = int(os.getenv("PROXY_COOLDOWN_SEC", "120") or "120")
            max_consec_fails = int(os.getenv("PROXY_MAX_CONSEC_FAILS", "3") or "3")
            sticky_reqs = int(os.getenv("PROXY_STICKY_REQUESTS", "3") or "3")
        except Exception:
            max_per_proxy, cooldown_sec, max_consec_fails, sticky_reqs = 2, 120, 3, 3
        self.proxy_pool = (
            ProxyPool(
                HTTP_PROXIES,
                max_concurrency_per_proxy=max_per_proxy,
                cooldown_sec=cooldown_sec,
                max_consec_fails=max_consec_fails,
                sticky_requests=sticky_reqs,
            )
            if HTTP_PROXIES
            else None
        )

        # Milvus connection
        self.connect_milvus()
        self.collection = self.get_or_create_collection()
        self.ensure_collection_ready()

        # Initialize existing URLs as empty - will be populated per college
        self.existing_urls = set()

        # Crawling statistics
        self.stats = {
            "total_pages_crawled": 0,
            "total_vectors_uploaded": 0,
            "total_errors": 0,
            "colleges_processed": 0,
            "duplicate_urls_skipped": 0,
            "existing_urls_skipped": 0,
        }

    def rotate_user_agent(self):
        """Rotate User-Agent to avoid detection."""
        new_user_agent = random.choice(self.user_agents)
        return new_user_agent

    def connect_milvus(self):
        """Connect to Zilliz Cloud database."""
        try:
            connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
            print("✓ Connected to Zilliz Cloud")
        except Exception as e:
            print(f"✗ Failed to connect to Zilliz Cloud: {e}")
            raise

    def get_or_create_collection(self):
        """Get or create the Zilliz Cloud collection."""
        collection_name = ZILLIZ_COLLECTION_NAME

        fields = [
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                is_primary=True,
                auto_id=False,
                max_length=36,
            ),
            FieldSchema(name="college_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="url", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(
                name="title", dtype=DataType.VARCHAR, max_length=MAX_TITLE_LENGTH
            ),
            FieldSchema(
                name="content", dtype=DataType.VARCHAR, max_length=MAX_CONTENT_LENGTH
            ),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
            FieldSchema(name="crawled_at", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="majors", dtype=DataType.JSON),
        ]

        schema = CollectionSchema(fields, description="College pages with embeddings")

        if utility.has_collection(collection_name):
            existing = Collection(collection_name)
            try:
                # Build expected field map
                expected = {f.name: f for f in schema.fields}
                actual = {f.name: f for f in existing.schema.fields}

                def varchar_len(f):
                    try:
                        return int(f.params.get("max_length"))
                    except Exception:
                        return None

                # Validate presence and compatibility for each expected field
                recreate = False
                for name, exp in expected.items():
                    if name not in actual:
                        print(f"⚠️ Missing field in existing collection: {name}")
                        recreate = True
                        continue
                    act = actual[name]
                    if act.dtype != exp.dtype:
                        print(
                            f"⚠️ Field dtype mismatch for '{name}': {act.dtype} != {exp.dtype}"
                        )
                        recreate = True
                        continue
                    if name == "embedding":
                        exp_dim = VECTOR_DIM
                        act_dim = act.params.get("dim")
                        if act_dim != exp_dim:
                            print(f"⚠️ Embedding dim mismatch: {act_dim} != {exp_dim}")
                            recreate = True
                    elif act.dtype == DataType.VARCHAR:
                        exp_len = varchar_len(exp)
                        act_len = varchar_len(act)
                        if (
                            act_len is not None
                            and exp_len is not None
                            and act_len < exp_len
                        ):
                            print(
                                f"⚠️ VARCHAR max_length for '{name}' too small: {act_len} < {exp_len}"
                            )
                            recreate = True

                # Also ensure primary key settings
                try:
                    id_field = actual.get("id")
                    if not id_field or not id_field.is_primary or id_field.auto_id:
                        print("⚠️ Primary key field 'id' is misconfigured")
                        recreate = True
                except Exception:
                    pass

                if recreate:
                    print(
                        "♻️ Recreating collection to match expected schema (this drops existing data)."
                    )
                    utility.drop_collection(collection_name)
                    return Collection(collection_name, schema)

                return existing
            except Exception as e:
                print(
                    f"⚠️ Could not verify existing collection schema: {e}. Proceeding with existing collection."
                )
                return existing
        return Collection(collection_name, schema)

    def ensure_collection_ready(self):
        """Create vector index if missing and load collection for querying/search."""
        try:
            # Create vector index if not present
            has_index = False
            try:
                idx = getattr(self.collection, "indexes", None)
                has_index = bool(idx)
            except Exception:
                has_index = False

            if not has_index:
                print("🔧 Creating vector index on 'embedding' field...")
                self.collection.create_index(
                    field_name="embedding",
                    index_params={
                        "index_type": INDEX_TYPE,
                        "metric_type": METRIC_TYPE,
                        "params": {"nlist": 1024},
                    },
                    timeout=600,
                )
                print("✅ Index creation requested")

            # Load collection to make it queryable
            try:
                self.collection.load(timeout=120)
                print("✅ Collection loaded")
            except Exception as e:
                print(f"⚠️  Could not load collection yet: {e}")
        except Exception as e:
            print(f"❌ Error ensuring collection readiness: {e}")

    def read_csv_files(self) -> Dict[str, List[Dict[str, str]]]:
        """
        Read all CSV files in the colleges directory and organize by major.

        Returns:
            Dictionary mapping major names to lists of college data
        """
        majors_data = {}

        # Find all CSV files in colleges directory
        csv_pattern = os.path.join(self.colleges_dir, "*.csv")
        csv_files = glob.glob(csv_pattern)

        if not csv_files:
            print(f"No CSV files found in {self.colleges_dir}")
            # Create a sample CSV file for demonstration
            self.create_sample_csv_files()
            csv_files = glob.glob(csv_pattern)

        for csv_file in csv_files:
            # Extract major name from filename (e.g., 'business.csv' -> 'business')
            major_name = os.path.splitext(os.path.basename(csv_file))[0]

            print(f"Reading {major_name} colleges from {csv_file}")

            colleges = []
            try:
                with open(csv_file, "r", encoding="utf-8", newline="") as f:
                    # Try to detect if file has headers
                    sample = f.read(1024)
                    f.seek(0)

                    # Check if file is empty or only whitespace
                    if not sample.strip():
                        print(f"Warning: {csv_file} is empty")
                        continue

                    reader = csv.DictReader(f)

                    # Handle different possible column names
                    fieldnames = reader.fieldnames
                    if not fieldnames:
                        print(f"Warning: {csv_file} has no headers")
                        continue

                    # Map common column variations
                    name_col = None
                    url_col = None

                    for field in fieldnames:
                        field_lower = field.lower().strip()
                        if field_lower in [
                            "name",
                            "college_name",
                            "university_name",
                            "school_name",
                        ]:
                            name_col = field
                        elif field_lower in [
                            "url",
                            "website",
                            "link",
                            "college_url",
                            "university_url",
                        ]:
                            url_col = field

                    if not name_col or not url_col:
                        print(
                            f"Warning: {csv_file} missing required columns (name/url)"
                        )
                        print(f"Available columns: {fieldnames}")
                        continue

                    for row in reader:
                        name = row.get(name_col, "").strip()
                        url = row.get(url_col, "").strip()

                        if name and url:
                            # Ensure URL has proper protocol
                            if not url.startswith(("http://", "https://")):
                                url = "https://" + url

                            colleges.append(
                                {"name": name, "url": url, "major": major_name}
                            )

                if colleges:
                    majors_data[major_name] = colleges
                    print(f"✓ Loaded {len(colleges)} colleges for {major_name}")
                else:
                    print(f"Warning: No valid college data found in {csv_file}")

            except Exception as e:
                print(f"Error reading {csv_file}: {e}")

        return majors_data

    def create_sample_csv_files(self):
        """Create sample CSV files for demonstration purposes."""
        print("Creating sample CSV files for demonstration...")

        sample_data = {
            "business.csv": [
                {"name": "Harvard Business School", "url": "https://www.hbs.edu/"},
                {
                    "name": "Stanford Graduate School of Business",
                    "url": "https://www.gsb.stanford.edu/",
                },
                {"name": "Wharton School", "url": "https://www.wharton.upenn.edu/"},
            ],
            "computer_science.csv": [
                {"name": "MIT EECS", "url": "https://www.eecs.mit.edu/"},
                {"name": "Stanford CS", "url": "https://cs.stanford.edu/"},
                {"name": "Carnegie Mellon SCS", "url": "https://www.cs.cmu.edu/"},
            ],
        }

        for filename, colleges in sample_data.items():
            csv_path = os.path.join(self.colleges_dir, filename)
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["name", "url"])
                writer.writeheader()
                writer.writerows(colleges)
            print(f"Created sample file: {csv_path}")

    def is_internal_link(self, url: str, base_url: str) -> bool:
        """Check if a URL is an internal link to the same domain."""
        try:
            parsed_url = urlparse(url)
            parsed_base = urlparse(base_url)

            # Handle relative URLs (no netloc means it's relative)
            if not parsed_url.netloc:
                return True

            # Normalize domains by removing www. prefix for comparison
            url_domain = parsed_url.netloc.lower().lstrip("www.")
            base_domain = parsed_base.netloc.lower().lstrip("www.")

            # Must be from the same domain or subdomain
            if url_domain != base_domain and not url_domain.endswith("." + base_domain):
                return False

            # Skip certain file types
            if any(url.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
                return False

            # Skip certain paths
            if any(skip_path in url.lower() for skip_path in SKIP_PATHS):
                return False

            # Skip fragments and javascript links
            if parsed_url.fragment and not parsed_url.path:
                return False

            if url.lower().startswith(("javascript:", "mailto:", "tel:", "ftp:")):
                return False

            return True

        except Exception as e:
            print(f"    Warning: Error parsing URL {url}: {e}")
            return False

    def normalize_url(self, url: str, base_url: Optional[str] = None) -> str:
        """Normalize URL by resolving relative, stripping trackers, sorting whitelisted queries, and collapsing slashes."""
        try:
            if base_url:
                url = urljoin(base_url, url)
            parsed = urlparse(url)
            scheme = parsed.scheme.lower() or "https"
            netloc = parsed.netloc.lower()
            # Remove default ports
            if netloc.endswith(":80") and scheme == "http":
                netloc = netloc[:-3]
            elif netloc.endswith(":443") and scheme == "https":
                netloc = netloc[:-4]
            # Collapse duplicate slashes in path
            path = re.sub(r"/{2,}", "/", parsed.path or "/")
            # Remove trailing slash unless root
            if path.endswith("/") and len(path) > 1:
                path = path.rstrip("/")
            # Strip trackers / session ids
            query_pairs = []
            for k, v in parse_qsl(parsed.query, keep_blank_values=True):
                kl = k.lower()
                if kl.startswith("utm_") or kl in {
                    "fbclid",
                    "gclid",
                    "msclkid",
                    "sessionid",
                    "phpsessid",
                }:
                    continue
                query_pairs.append((k, v))
            # Sort whitelisted query params for stability
            query_pairs.sort(key=lambda kv: kv[0])
            query = urlencode(query_pairs, doseq=True)
            # Drop fragments
            normalized = f"{scheme}://{netloc}{path}"
            if query:
                normalized += f"?{query}"
            return normalized
        except Exception:
            return url

    def extract_internal_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        """Extract all internal links from a BeautifulSoup object."""
        links = set()  # Use set for automatic deduplication

        # Find all anchor tags with href attributes
        for link in soup.find_all("a", href=True):
            href = link.get("href")
            if not href or not href.strip():
                continue

            href = href.strip()

            # Skip empty fragments and anchor-only links
            if href == "#" or href.startswith("#"):
                continue

            try:
                # Convert relative URLs to absolute
                absolute_url = self.normalize_url(href, base_url)

                # Normalize URL by removing fragments and trailing slashes
                normalized_url = absolute_url

                # Check if it's an internal link
                if self.is_internal_link(normalized_url, base_url):
                    links.add(normalized_url)

            except Exception as e:
                print(f"    Warning: Error processing link '{href}': {e}")
                continue

        # Also consider <link rel="next"> pagination hints
        try:
            next_link = soup.find("link", rel=lambda v: v and "next" in str(v).lower())
            if next_link and next_link.get("href"):
                try:
                    absolute_next = urljoin(base_url, next_link.get("href"))
                    parsed = urlparse(absolute_next)
                    normalized_next = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if parsed.query:
                        normalized_next += f"?{parsed.query}"
                    if self.is_internal_link(normalized_next, base_url):
                        links.add(normalized_next)
                except Exception:
                    pass
        except Exception:
            pass

        return list(links)

    def scrape_page(
        self, url: str, session: requests.Session = None
    ) -> Optional[Dict[str, Any]]:
        """Scrape a single page and return structured data."""
        try:
            print(f"    Crawling: {url}")

            # Add small delay between requests to be respectful
            time.sleep(random.uniform(self.min_delay, self.max_delay))

            # Per-host token bucket and circuit breaker
            try:
                netloc = urlparse(url).netloc
            except Exception:
                netloc = ""
            # Circuit breaker check
            with self._host_lock:
                cb_until = self._host_circuit_until.get(netloc, 0.0)
                now = time.time()
                if now < cb_until:
                    wait_left = cb_until - now
                    print(
                        f"    ⏳ Host {netloc} in cooldown ({wait_left:.1f}s). Skipping {url}"
                    )
                    return None
                # Refill tokens
                bucket = self._host_tokens.get(netloc)
                if not bucket:
                    bucket = {
                        "tokens": float(self.max_tokens_per_host),
                        "last": now,
                    }
                    self._host_tokens[netloc] = bucket
                else:
                    elapsed = now - bucket["last"]
                    bucket["tokens"] = min(
                        float(self.max_tokens_per_host),
                        bucket["tokens"] + elapsed * self.token_refill_per_sec,
                    )
                    bucket["last"] = now
                if bucket["tokens"] < 1.0:
                    # Not enough tokens; delay slightly
                    need = 1.0 - bucket["tokens"]
                    delay_s = need / max(0.1, self.token_refill_per_sec)
                    time.sleep(min(1.0, delay_s))
            # Use provided session or fall back to shared session
            request_session = session or self.session

            # Fetch the page with retry logic for 403 errors
            response = None
            for attempt in range(self.max_retries):
                try:
                    # Light probabilistic proxy usage on first attempt, rotate on retries
                    proxy_dict = None
                    proxy_token = None
                    selected_proxy_url = None
                    sticky_key = (urlparse(url).netloc, threading.get_ident())
                    if self.proxy_pool:
                        # acquire best available proxy
                        selected_proxy_url, proxy_token = self.proxy_pool.acquire(
                            netloc=sticky_key[0], sticky_key=sticky_key
                        )
                        if selected_proxy_url:
                            proxy_dict = {
                                "http": selected_proxy_url,
                                "https": selected_proxy_url,
                            }

                    # Consume one host token for this attempt
                    with self._host_lock:
                        bucket2 = self._host_tokens.get(netloc)
                        if bucket2:
                            bucket2["tokens"] = max(0.0, bucket2["tokens"] - 1.0)

                    if USE_CURL_CFFI and curl_requests is not None and attempt >= 2:
                        response = curl_requests.get(
                            url,
                            impersonate="chrome",
                            headers=request_session.headers,
                            proxies=proxy_dict,
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=True,
                        )
                    else:
                        response = request_session.get(
                            url,
                            timeout=REQUEST_TIMEOUT,
                            allow_redirects=True,
                            proxies=proxy_dict,
                        )

                    # Handle 403 errors specifically
                    if response.status_code == 403:
                        print(
                            f"    ⚠️  403 Forbidden for {url} (attempt {attempt + 1}/{self.max_retries})"
                        )
                        if attempt < self.max_retries - 1:
                            # Rotate User-Agent and wait longer for 403 errors before retry
                            new_ua = self.rotate_user_agent()
                            # Ensure the worker's session also gets the rotated UA
                            try:
                                request_session.headers.update({"User-Agent": new_ua})
                            except Exception:
                                pass
                            print(f"    🔄 Rotated User-Agent to: {new_ua[:50]}...")
                            backoff = self.delay * (attempt + 1) * 2
                            if proxy_dict:
                                backoff *= 1.5
                            time.sleep(backoff)
                            continue
                        else:
                            print(
                                f"    ✗ Giving up on {url} after {self.max_retries} attempts due to 403"
                            )
                            return None

                    response.raise_for_status()
                    # inform proxy pool of success
                    if self.proxy_pool and proxy_token is not None:
                        self.proxy_pool.release(
                            proxy_token,
                            success=True,
                            status_code=response.status_code,
                            latency_ms=max(
                                0.0,
                                (
                                    time.monotonic()
                                    - proxy_token.get("start", time.monotonic())
                                )
                                * 1000.0,
                            ),
                        )
                    # Update host success and adjust concurrency upwards slowly
                    with self._host_lock:
                        self._host_failures[netloc] = 0
                        cur = self._host_concurrency.get(
                            netloc, self.min_concurrency_per_host
                        )
                        if (
                            cur < self.max_concurrency_per_host
                            and random.random() < 0.1
                        ):
                            self._host_concurrency[netloc] = cur + 1
                    break

                except requests.exceptions.HTTPError as e:
                    if "403" in str(e):
                        print(
                            f"    ⚠️  403 Forbidden for {url} (attempt {attempt + 1}/{self.max_retries})"
                        )
                        if attempt < self.max_retries - 1:
                            # Rotate User-Agent and wait longer for 403 errors before retry
                            new_ua = self.rotate_user_agent()
                            # Ensure the worker's session also gets the rotated UA
                            try:
                                request_session.headers.update({"User-Agent": new_ua})
                            except Exception:
                                pass
                            print(f"    🔄 Rotated User-Agent to: {new_ua[:50]}...")
                            backoff = self.delay * (attempt + 1) * 2
                            if proxy_dict:
                                backoff *= 1.5
                            time.sleep(backoff)
                            continue
                        else:
                            print(
                                f"    ✗ Giving up on {url} after {self.max_retries} attempts due to 403"
                            )
                            # mark proxy as failed if used
                            if self.proxy_pool and proxy_token is not None:
                                self.proxy_pool.release(
                                    proxy_token,
                                    success=False,
                                    status_code=403,
                                )
                            # Circuit breaker on 403
                            with self._host_lock:
                                fails = self._host_failures.get(netloc, 0) + 1
                                self._host_failures[netloc] = fails
                                if fails >= 3:
                                    self._host_circuit_until[netloc] = (
                                        time.time() + min(300, 30 * fails)
                                    )
                            return None
                    else:
                        # for other HTTP errors, mark failure then re-raise to outer except
                        if self.proxy_pool and proxy_token is not None:
                            try:
                                code = getattr(e.response, "status_code", None)
                            except Exception:
                                code = None
                            self.proxy_pool.release(
                                proxy_token,
                                success=False,
                                status_code=code,
                            )
                        # adaptive backoff + circuit breaker increments
                        with self._host_lock:
                            fails = self._host_failures.get(netloc, 0) + 1
                            self._host_failures[netloc] = fails
                            if code in {429, 408, 500, 502, 503, 504} and fails >= 3:
                                self._host_circuit_until[netloc] = time.time() + min(
                                    300, 20 * fails
                                )
                        raise e
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        # final failure
                        if self.proxy_pool and proxy_token is not None:
                            self.proxy_pool.release(
                                proxy_token,
                                success=False,
                                status_code=None,
                                error=e,
                            )
                        with self._host_lock:
                            fails = self._host_failures.get(netloc, 0) + 1
                            self._host_failures[netloc] = fails
                            if fails >= 3:
                                self._host_circuit_until[netloc] = time.time() + min(
                                    300, 20 * fails
                                )
                        raise e
                    # bounded backoff with jitter
                    time.sleep(self.delay * (attempt + 1) * random.uniform(0.8, 1.3))
                    continue
                finally:
                    # ensure capacity released on early continues
                    if self.proxy_pool and proxy_token is not None:
                        # ensure semaphore released if not yet
                        try:
                            self.proxy_pool.release(proxy_token, success=True)
                        except Exception:
                            pass

            if not response:
                return None

            # Validate content-type before parsing
            try:
                content_type_header = response.headers.get("Content-Type", "")
            except Exception:
                content_type_header = ""
            mime_type = (
                content_type_header.split(";")[0].strip().lower()
                if content_type_header
                else ""
            )
            needs_pw = False
            try:
                if mime_type and (mime_type not in VALID_CONTENT_TYPES):
                    print(
                        f"    ⚠️  Skipping non-HTML content-type for {url}: {mime_type}"
                    )
                    # Consider JS-rendered fallback for non-HTML or mismatched types
                    if self.playwright_enabled and sync_playwright is not None:
                        needs_pw = True
                    # Try to proceed in case servers mislabel content-type
                    # Fallback to parsing anyway to avoid missing pages due to mislabeled headers
            except Exception:
                pass

            # Parse with BeautifulSoup
            soup = BeautifulSoup(response.content, "html.parser")

            # Extract title
            title = soup.find("title")
            title_text = title.get_text(strip=True) if title else ""

            # Extract main content
            # Remove script, style, nav, footer, header elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()

            # Try to find main content areas
            main_content = ""
            main_selectors = [
                "main",
                "article",
                '[role="main"]',
                ".main-content",
                ".content",
                "#content",
                ".post-content",
                ".entry-content",
            ]

            for selector in main_selectors:
                main_element = soup.select_one(selector)
                if main_element:
                    main_content = main_element.get_text(separator=" ", strip=True)
                    break

            # Fallback to body if no main content found
            if not main_content:
                body = soup.find("body")
                if body:
                    main_content = body.get_text(separator=" ", strip=True)

            # Clean the content
            cleaned_content = clean_text(main_content)

            # Check if we have meaningful content using config thresholds
            word_count = len(cleaned_content.split())
            try:
                min_chars = MIN_CONTENT_LENGTH
            except Exception:
                min_chars = 0
            try:
                min_words = MIN_WORDS_PER_PAGE
            except Exception:
                min_words = 0
            if len(cleaned_content.strip()) < max(1, min_chars) or word_count < max(
                1, min_words
            ):
                print(
                    f"    ⚠️  Insufficient content for {url} (chars={len(cleaned_content)}, words={word_count})"
                )
                # Suggest JS-rendered fallback if content looks incomplete
                if self.playwright_enabled and sync_playwright is not None:
                    needs_pw = True
                # Do not return early; still extract internal links so BFS can progress

            # Extract internal links
            internal_links = self.extract_internal_links(soup, url)
            # Normalize discovered links
            internal_links = [self.normalize_url(u) for u in internal_links]
            # Heuristic: if no links found and Playwright is available, try JS fallback once
            if (
                not internal_links
                and self.playwright_enabled
                and sync_playwright is not None
            ):
                needs_pw = True

            # Debug info for stuck crawler
            if len(internal_links) > 0:
                print(f"    🔗 Found {len(internal_links)} internal links for {url}")

            return {
                "url": url,
                "title": title_text,
                "content": cleaned_content,
                "internal_links": internal_links,
                "word_count": word_count,
                "crawled_at": datetime.now().isoformat(),
                "needs_pw": needs_pw,
            }

        except Exception as e:
            print(f"    ✗ Error scraping {url}: {e}")
            return None

    def _scrape_with_playwright(self, url: str) -> Optional[Dict[str, Any]]:
        """Render page with Playwright and extract title/content/links. Best-effort and bounded.

        This is a sync helper guarded by a semaphore to cap concurrency.
        """
        if not self.playwright_enabled or sync_playwright is None:
            return None
        try:
            with self.playwright_semaphore:
                # Start or reuse a shared Playwright runtime
                if self._pw is None:
                    self._pw = sync_playwright().start()
                p = self._pw
                # Acquire proxy for Playwright (optional)
                pw_proxy_token = None
                pw_proxy_settings = None
                try:
                    if self.proxy_pool:
                        netloc = urlparse(url).netloc
                        sticky_key = (netloc, "pw")
                        selected_proxy_url, pw_proxy_token = self.proxy_pool.acquire(
                            netloc=netloc, sticky_key=sticky_key
                        )
                        if selected_proxy_url:
                            parsed = urlparse(selected_proxy_url)
                            server = (
                                f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                            )
                            pw_proxy_settings = {"server": server}
                            if parsed.username or parsed.password:
                                if parsed.username:
                                    pw_proxy_settings["username"] = parsed.username
                                if parsed.password:
                                    pw_proxy_settings["password"] = parsed.password
                except Exception:
                    pw_proxy_settings = None

                    pw_start = time.monotonic()
                    # Reuse a shared Chromium browser per proxy key
                    browser_key = (
                        browser_key
                        if "browser_key" in locals()
                        else (selected_proxy_url or "direct")
                    )
                    with self._pw_browser_lock:
                        browser = self._pw_browsers.get(browser_key)
                        if browser is None:
                            browser = p.chromium.launch(
                                headless=True, proxy=pw_proxy_settings
                            )
                            self._pw_browsers[browser_key] = browser
                    # Diversify device/locale/timezone per run to reduce bot fingerprinting
                    ua = random.choice(self.user_agents)
                    locales = ["en-US", "en-GB", "en-CA"]
                    timezone_ids = ["America/New_York", "America/Los_Angeles", "UTC"]
                    viewport_opts = [(1280, 800), (1366, 768), (1920, 1080)]
                    vw, vh = random.choice(viewport_opts)
                    context = browser.new_context(
                        user_agent=ua,
                        java_script_enabled=True,
                        locale=random.choice(locales),
                        timezone_id=random.choice(timezone_ids),
                        viewport={"width": vw, "height": vh},
                    )
                    page = context.new_page()

                    # Speed up by blocking non-essential resources
                    def route_filter(route):
                        req = route.request
                        if req.resource_type in {"image", "font", "media"}:
                            return route.abort()
                        return route.continue_()

                    context.route("**/*", route_filter)
                    page.set_default_timeout(self.playwright_nav_timeout_ms)
                    # Snapshot at DOMContentLoaded and after short idle
                    html_dom = ""
                    html_idle = ""
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        try:
                            html_dom = page.content()
                        except Exception:
                            html_dom = ""
                        # Try to accept cookie banners quickly
                        self._try_accept_cookies(page)
                        # Apply per-domain profile actions
                        try:
                            netloc2 = urlparse(url).netloc
                        except Exception:
                            netloc2 = ""
                        if netloc2:
                            profile = self._load_playwright_profile(netloc2)
                            if profile:
                                self._apply_playwright_profile(page, profile)
                        # Wait for network to settle a bit
                        page.wait_for_load_state(
                            "networkidle", timeout=self.playwright_nav_timeout_ms
                        )
                        try:
                            html_idle = page.content()
                        except Exception:
                            html_idle = ""
                    except PlaywrightTimeoutError:
                        pass
                    finally:
                        # Ensure per-page resources are released
                        try:
                            page.close()
                        except Exception:
                            pass
                        try:
                            context.close()
                        except Exception:
                            pass

                    # Do not close shared browser instance

                    # Update proxy pool with success
                    if self.proxy_pool and pw_proxy_token is not None:
                        try:
                            self.proxy_pool.release(
                                pw_proxy_token,
                                success=True,
                                status_code=None,
                                latency_ms=max(
                                    0.0, (time.monotonic() - pw_start) * 1000.0
                                ),
                            )
                        except Exception:
                            pass

            # Choose best snapshot and build soups
            soup_dom = BeautifulSoup(html_dom or "", "html.parser")
            soup_idle = BeautifulSoup(html_idle or "", "html.parser")
            # Remove common consent overlays
            cookie_selectors = [
                '[id*="cookie"]',
                '[class*="cookie"]',
                '[id*="consent"]',
                '[class*="consent"]',
                '[id*="gdpr"]',
                '[class*="gdpr"]',
                "#onetrust-banner-sdk",
                "#onetrust-consent-sdk",
                "#truste-consent-track",
                ".truste_overlay",
                "#qc-cmp2-ui",
                "#sp-cc",
                ".sp_choice_type",
                "#CybotCookiebotDialog",
            ]
            for sel in cookie_selectors:
                for el in soup_dom.select(sel):
                    el.decompose()
                for el in soup_idle.select(sel):
                    el.decompose()

            # Pick better title/content
            def extract_text_and_links(soup_obj: BeautifulSoup) -> tuple:
                if not soup_obj:
                    return "", []
                for element in soup_obj(["script", "style", "nav", "footer", "header"]):
                    element.decompose()
                main_selectors_local = [
                    "main",
                    "article",
                    '[role="main"]',
                    ".main-content",
                    ".content",
                    "#content",
                    ".post-content",
                    ".entry-content",
                ]
                main_content_local = ""
                for selector in main_selectors_local:
                    main_element = soup_obj.select_one(selector)
                    if main_element:
                        main_content_local = main_element.get_text(
                            separator=" ", strip=True
                        )
                        break
                if not main_content_local:
                    body = soup_obj.find("body")
                    if body:
                        main_content_local = body.get_text(separator=" ", strip=True)
                cleaned_local = clean_text(main_content_local)
                links_local = self.extract_internal_links(soup_obj, url)
                return cleaned_local, links_local

            cleaned_dom, links_dom = extract_text_and_links(soup_dom)
            cleaned_idle, links_idle = extract_text_and_links(soup_idle)

            title_dom = soup_dom.find("title") if soup_dom else None
            title_idle = soup_idle.find("title") if soup_idle else None
            title_text = (
                title_idle.get_text(strip=True)
                if title_idle
                else (title_dom.get_text(strip=True) if title_dom else "")
            )

            chosen_content = (
                cleaned_idle if len(cleaned_idle) >= len(cleaned_dom) else cleaned_dom
            )
            internal_links = list({*links_dom, *links_idle})
            word_count = len(chosen_content.split())
            return {
                "url": url,
                "title": title_text,
                "content": chosen_content,
                "internal_links": internal_links,
                "word_count": word_count,
                "crawled_at": datetime.now().isoformat(),
            }
        except Exception as e:
            # Update proxy pool with failure
            try:
                if (
                    "pw_proxy_token" in locals()
                    and pw_proxy_token is not None
                    and self.proxy_pool
                ):
                    self.proxy_pool.release(
                        pw_proxy_token,
                        success=False,
                        status_code=None,
                        error=e,
                    )
            except Exception:
                pass
            print(f"    ⚠️  Playwright fallback failed for {url}: {e}")
            return None

    def _try_accept_cookies(self, page) -> None:
        """Best-effort cookie banner acceptor for common frameworks."""
        try:
            selectors = [
                "#onetrust-accept-btn-handler",
                "#CybotCookiebotDialogBodyLevelButtonAccept",
                "#sp-cc-accept",
                'button:has-text("Accept all")',
                'button:has-text("I agree")',
            ]
            for sel in selectors:
                try:
                    btn = page.locator(sel).first
                    if btn and btn.is_visible(timeout=1500):
                        btn.click(timeout=1500)
                        break
                except Exception:
                    continue
        except Exception:
            pass

    def _load_playwright_profile(self, netloc: str) -> Dict[str, Any]:
        """Load a YAML profile for a given domain (netloc) with caching. Thread-safe.

        Merges with default.yml if present; domain overrides take precedence.
        """
        # Use lock to protect cache updates
        with self._pw_profile_cache_lock:
            if netloc in self._pw_profile_cache:
                return self._pw_profile_cache[netloc]
        # Outside lock for IO
        default_path = os.path.join(self.playwright_profiles_dir, "default.yml")
        profile_path = os.path.join(self.playwright_profiles_dir, f"{netloc}.yml")
        data: Dict[str, Any] = {}
        try:
            # Load default first
            if os.path.exists(default_path):
                with open(default_path, "r", encoding="utf-8") as f:
                    base = yaml.safe_load(f) or {}
                    if isinstance(base, dict):
                        data.update(base)
            if os.path.exists(profile_path):
                with open(profile_path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                    if isinstance(loaded, dict):
                        data.update(loaded)
        except Exception as e:
            print(f"    ⚠️  Failed to load profile for {netloc}: {e}")
        # Cache the result (even empty) for this run
        with self._pw_profile_cache_lock:
            self._pw_profile_cache[netloc] = data
        return data

    def _apply_playwright_profile(self, page, profile: Dict[str, Any]) -> None:
        """Apply generic scripted actions from a profile (clicks, scrolls, waits)."""
        try:
            # Click actions (e.g., load more, expand accordions)
            for sel in profile.get("click_selectors", []) or []:
                try:
                    loc = page.locator(sel).first
                    if loc and loc.is_visible(timeout=1500):
                        loc.click(timeout=1500)
                except Exception:
                    continue
            # Pagination: click next up to N times (bounded)
            next_sel = profile.get("pagination_selector")
            max_pages = int(profile.get("pagination_max", 0) or 0)
            for _ in range(max(0, max_pages)):
                try:
                    loc = page.locator(next_sel).first
                    if loc and loc.is_visible(timeout=1500):
                        loc.click(timeout=1500)
                        page.wait_for_load_state(
                            "networkidle", timeout=self.playwright_nav_timeout_ms
                        )
                    else:
                        break
                except Exception:
                    break
            # Bounded infinite scroll
            scroll_loops = int(profile.get("scroll_loops", 0) or 0)
            for _ in range(max(0, scroll_loops)):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.75)
                except Exception:
                    break
        except Exception:
            pass

    def upload_to_milvus(
        self, page_data: Dict[str, Any], college_name: str, major: str
    ) -> bool:
        """Upload a single page to Milvus with per-chunk embeddings for RAG."""
        try:
            # Fetch existing records for this URL
            existing_records = []
            try:
                # Escape quotes for Milvus boolean expression
                _url_val = page_data["url"].replace('"', '\\"')
                with self.collection_query_sema:
                    existing_records = self.collection.query(
                        expr=f'url == "{_url_val}"',
                        output_fields=[
                            "id",
                            "college_name",
                            "url",
                            "title",
                            "content",
                            "embedding",
                            "crawled_at",
                            "majors",
                        ],
                        limit=16384,
                    )
            except Exception as e:
                print(f"    ⚠️  Could not query existing URL '{page_data['url']}': {e}")

            # Determine if current major already present; if not, update majors in-place via upsert
            if existing_records:
                # Aggregate majors per record and determine if ANY record is missing the major
                all_have_major = True
                updated_ids: List[str] = []
                updated_colleges: List[str] = []
                updated_urls: List[str] = []
                updated_titles: List[str] = []
                updated_contents: List[str] = []
                updated_embeddings: List[List[float]] = []
                updated_crawled_ats: List[str] = []
                updated_majors_col: List[List[str]] = []

                for rec in existing_records:
                    rec_majors_field = rec.get("majors")
                    if isinstance(rec_majors_field, list):
                        rec_majors = [str(m).strip() for m in rec_majors_field if m]
                    elif (
                        isinstance(rec_majors_field, dict)
                        and "list" in rec_majors_field
                    ):
                        rec_majors = [
                            str(m).strip()
                            for m in rec_majors_field.get("list", [])
                            if m
                        ]
                    else:
                        rec_majors = []

                    if major not in rec_majors:
                        all_have_major = False
                        rec_majors = list({*rec_majors, major})

                    # Collect row for upsert (even if unchanged, harmless)
                    updated_ids.append(rec.get("id"))
                    updated_colleges.append(rec.get("college_name", college_name))
                    updated_urls.append(rec.get("url", page_data["url"]))
                    updated_titles.append(rec.get("title", page_data["title"]))
                    updated_contents.append(rec.get("content", page_data["content"]))
                    emb = rec.get("embedding")
                    if isinstance(emb, list) and len(emb) == VECTOR_DIM:
                        updated_embeddings.append(emb)
                    else:
                        # If embedding missing (shouldn't happen), skip update for safety
                        updated_embeddings.append([0.0] * VECTOR_DIM)
                    updated_crawled_ats.append(
                        rec.get("crawled_at", page_data["crawled_at"])
                    )
                    updated_majors_col.append(rec_majors)

                if all_have_major:
                    print(
                        f"    ⚠️  Skipping URL with existing matching major across all chunks: {page_data['url']} [{major}]"
                    )
                    # Count skip and allow worker to continue; do not block caller
                    with self.lock:
                        self.stats["existing_urls_skipped"] += 1
                    return False

                # Upsert updated majors for all rows of this URL
                try:
                    with self.collection_write_lock:
                        if hasattr(self.collection, "upsert"):
                            self.collection.upsert(
                                [
                                    updated_ids,
                                    updated_colleges,
                                    updated_urls,
                                    updated_titles,
                                    updated_contents,
                                    updated_embeddings,
                                    updated_crawled_ats,
                                    updated_majors_col,
                                ]
                            )
                        else:
                            # Fallback: delete old ids and insert updated rows
                            quoted = ",".join([f'"{_id}"' for _id in updated_ids])
                            self.collection.delete(f"id in [{quoted}]")
                            self.collection.insert(
                                [
                                    updated_ids,
                                    updated_colleges,
                                    updated_urls,
                                    updated_titles,
                                    updated_contents,
                                    updated_embeddings,
                                    updated_crawled_ats,
                                    updated_majors_col,
                                ]
                            )
                    print(
                        f"    ✓ Updated majors for existing URL across all chunks (added '{major}'): {page_data['url']}"
                    )
                except Exception as e:
                    print(f"    ✗ Failed to update majors for {page_data['url']}: {e}")
                    return False

                # Count as updated but no new vectors added
                return True

            # Chunk content and embed per chunk for better RAG retrieval
            title_text = page_data["title"]
            content_text = page_data["content"]
            chunks = chunk_text_by_tokens(
                content_text,
                max_tokens=800,
                overlap_tokens=80,
                model="text-embedding-ada-002",
            )
            # Bound embedding concurrency across workers and avoid duplicative re-chunking
            with self.embed_semaphore:
                chunk_inputs = [
                    f"{title_text}\n\n{c}" if title_text else c for c in chunks
                ]
                chunks_embeddings = get_embeddings_batch(
                    chunk_inputs, model="text-embedding-ada-002"
                )
            # If chunking produced no embeddings, fall back to whole-page embedding
            if not chunks_embeddings:
                with self.embed_semaphore:
                    fallback_emb = get_embedding(f"{title_text} {content_text}")
                if not fallback_emb:
                    print(f"    ✗ Failed to generate embedding for {page_data['url']}")
                    return False
                chunks_embeddings = [fallback_emb]
                chunks = [content_text]

            # Prepare data for Milvus matching the new schema

            # Create a combined description from title and content
            description = f"{page_data['title']} {page_data['content']}"
            if len(description) > 2047:  # Leave room for null terminator
                description = description[:2047]

            # Prepare column-based insert payload (more compatible across PyMilvus versions)
            ids: List[str] = []
            colleges: List[str] = []
            urls: List[str] = []
            titles: List[str] = []
            contents: List[str] = []
            embeddings: List[List[float]] = []
            crawled_ats: List[str] = []
            majors: List[str] = []

            total_chunks = len(chunks_embeddings)
            for idx, (emb, chunk_text) in enumerate(
                zip(chunks_embeddings, chunks), start=1
            ):
                if emb is None:
                    continue
                if not isinstance(emb, list) or len(emb) != VECTOR_DIM:
                    print(
                        f"    ✗ Skipping invalid embedding for {page_data['url']} (dim={len(emb) if isinstance(emb, list) else 'N/A'})"
                    )
                    continue
                chunked_title = page_data["title"]
                if total_chunks > 1:
                    chunked_title = f"{page_data['title']} (chunk {idx}/{total_chunks})"
                ids.append(str(uuid.uuid4()))
                colleges.append(college_name)
                urls.append(page_data["url"])
                titles.append(chunked_title[: MAX_TITLE_LENGTH - 1])
                contents.append(chunk_text[: MAX_CONTENT_LENGTH - 1])
                embeddings.append(emb)
                crawled_ats.append(page_data["crawled_at"])
                # Multi-major support
                majors.append([major])

            # Insert into Milvus
            if embeddings:
                try:
                    # Serialize inserts/queries for safety across threads
                    with self.collection_write_lock:
                        self.collection.insert(
                            [
                                ids,
                                colleges,
                                urls,
                                titles,
                                contents,
                                embeddings,
                                crawled_ats,
                                majors,
                            ]
                        )
                except Exception as insert_err:
                    print(f"    ✗ Insert failed for {page_data['url']}: {insert_err}")
                    return False

            with self.lock:
                # Count vectors uploaded for more accurate stats in RAG mode
                self.stats["total_vectors_uploaded"] += len(embeddings)

            print(
                f"    ✓ Uploaded {len(embeddings)} vector(s) to Milvus: {page_data['url']}"
            )
            return True

        except Exception as e:
            print(f"    ✗ Error uploading to Milvus: {e}")
            with self.lock:
                self.stats["total_errors"] += 1
            return False

    def crawl_college_site(
        self, college: Dict[str, str], max_pages: int = None
    ) -> Dict[str, Any]:
        """Crawl a single college website using efficient BFS with work-stealing."""
        college_name = college["name"]
        base_url = college["url"]
        major = college["major"]
        max_pages = max_pages or MAX_PAGES_PER_COLLEGE

        print(f"\n=== Crawling {college_name} ({major}) ===")
        print(f"Base URL: {base_url}")

        # (Deprecated) existing URL cache is not used for skipping; major-aware checks happen at upload time
        self.existing_urls = set()

        # Reset state for this college (shared across workers)
        crawled_urls = set()
        discovered_urls = set()
        state_lock = threading.Lock()
        stop_event = threading.Event()
        pages_crawled_shared = 0  # successful pages scraped

        # Normalize base URL
        try:
            parsed = urlparse(base_url)
            normalized_base = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            normalized_base = base_url

        # Test the base URL first to check if the site is accessible
        print(f"    Testing base URL: {normalized_base}")
        test_page = self.scrape_page(normalized_base, self.session)
        if not test_page:
            print(
                f"    ✗ Cannot access {college_name} - site may be blocked or unavailable"
            )
            print(f"    ⏭️  Moving to next college...")
            return {
                "college_name": college_name,
                "major": major,
                "base_url": base_url,
                "pages_crawled": 0,
                "pages_uploaded": 0,
                "urls_discovered": 0,
                "status": "blocked",
            }

        # Initialize BFS queue
        discovered_urls.add(normalized_base)

        pages_crawled = 0
        pages_uploaded = 0

        # Use ThreadPoolExecutor with true work-stealing
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Dedicated executor for Playwright fallback jobs (bounded workers)
            pw_executor = ThreadPoolExecutor(max_workers=self.playwright_max_workers)
            # Use a real queue for thread-safe operations
            from queue import Queue

            work_queue = Queue()
            work_queue.put((0, normalized_base))

            # Track active futures
            active_futures = set()

            def worker_task():
                """Efficient worker for work-stealing BFS"""
                nonlocal pages_crawled_shared
                local_crawled = 0
                local_uploaded = 0
                consecutive_empty_checks = 0
                max_empty_checks = 10  # Exit if queue is empty for too long

                # Create thread-local session for thread safety
                worker_session = requests.Session()
                worker_session.headers.update(self.session.headers)

                while not stop_event.is_set():
                    # Check global stop condition early
                    with state_lock:
                        if pages_crawled_shared >= max_pages:
                            stop_event.set()
                            break
                    try:
                        # Get next URL with timeout
                        depth, url = work_queue.get(timeout=1.0)  # Increased timeout
                        consecutive_empty_checks = 0  # Reset counter

                        # Claim URL atomically
                        with state_lock:
                            if stop_event.is_set():
                                break
                            if url in crawled_urls:
                                with self.lock:
                                    self.stats["duplicate_urls_skipped"] += 1
                                continue
                            crawled_urls.add(url)

                        # Scrape page with thread-local session
                        page_data = self.scrape_page(url, worker_session)
                        if not page_data:
                            continue

                        local_crawled += 1
                        # Update global count and check for stop condition
                        with state_lock:
                            pages_crawled_shared += 1
                            if pages_crawled_shared >= max_pages:
                                stop_event.set()

                        # Upload to Milvus unless we plan a PW fallback upload
                        if not page_data.get("needs_pw"):
                            if self.upload_to_milvus(page_data, college_name, major):
                                local_uploaded += 1

                        # If this URL needs Playwright, offload the job and merge results asynchronously
                        if page_data.get("needs_pw"):

                            def _merge_pw_result(fut, new_depth=new_depth, src_url=url):
                                try:
                                    result = fut.result()
                                except Exception:
                                    return
                                if not result:
                                    return
                                if stop_event.is_set():
                                    return
                                # Upload PW-rendered page
                                try:
                                    self.upload_to_milvus(result, college_name, major)
                                except Exception:
                                    pass
                                # Enqueue discovered links
                                links = result.get("internal_links", [])
                                for link in links:
                                    norm = self.normalize_url(link)
                                    with state_lock:
                                        if stop_event.is_set():
                                            break
                                        if (
                                            norm not in crawled_urls
                                            and norm not in discovered_urls
                                            and pages_crawled_shared < max_pages
                                        ):
                                            discovered_urls.add(norm)
                                            work_queue.put((new_depth, norm))
                                return

                            try:
                                fut = pw_executor.submit(
                                    self._scrape_with_playwright, url
                                )
                                fut.add_done_callback(_merge_pw_result)
                            except Exception:
                                pass
                        # Add new links to queue (BFS)
                        new_links = page_data.get("internal_links", [])
                        new_depth = depth + 1
                        links_added = 0

                        # Filter and enqueue new links atomically
                        for link in new_links:
                            with state_lock:
                                if stop_event.is_set():
                                    break
                                already_seen = (
                                    link in crawled_urls or link in discovered_urls
                                )
                                if (
                                    not already_seen
                                    and pages_crawled_shared < max_pages
                                ):
                                    discovered_urls.add(link)
                                    work_queue.put((new_depth, link))
                                    links_added += 1
                                elif already_seen:
                                    with self.lock:
                                        self.stats["duplicate_urls_skipped"] += 1

                        # Progress update
                        if local_crawled % 5 == 0:  # More frequent updates
                            print(
                                f"    {college_name}: {local_crawled} crawled, {local_uploaded} uploaded, {links_added} new links found"
                            )

                    except queue.Empty:
                        consecutive_empty_checks += 1
                        print(
                            f"    ⚠️  Queue empty for {college_name} (check {consecutive_empty_checks}/{max_empty_checks})"
                        )

                        # Exit if queue has been empty for too long
                        if (
                            consecutive_empty_checks >= max_empty_checks
                            or stop_event.is_set()
                        ):
                            print(
                                f"    ⏭️  Exiting worker for {college_name} - queue empty for too long"
                            )
                            # signal others to exit
                            stop_event.set()
                            break

                        # Check if we should exit
                        with state_lock:
                            if pages_crawled_shared >= max_pages:
                                stop_event.set()
                                break
                        continue
                    except Exception as e:
                        print(f"    ✗ Worker error for {college_name}: {e}")
                        continue

                # Close thread-local session before exiting
                try:
                    worker_session.close()
                except Exception:
                    pass
                return local_crawled, local_uploaded

            # Submit initial workers
            num_workers = min(
                self.max_workers, max_pages, 20
            )  # Cap at 20 for efficiency

            for i in range(num_workers):
                future = executor.submit(worker_task)
                active_futures.add(future)

            # Monitor progress
            try:
                start_time = time.time()
                max_crawl_time = MAX_CRAWL_TIME_PER_COLLEGE

                while active_futures and not stop_event.is_set():
                    # Check for timeout
                    if time.time() - start_time > max_crawl_time:
                        print(
                            f"    ⏰ Timeout reached for {college_name} ({max_crawl_time}s)"
                        )
                        stop_event.set()
                        break

                    done, active_futures = wait(
                        active_futures,
                        return_when=FIRST_COMPLETED,
                        timeout=2.0,  # Increased timeout
                    )

                    for future in done:
                        try:
                            worker_crawled, worker_uploaded = future.result()

                            # Thread-safe statistics updates
                            pages_crawled += worker_crawled
                            pages_uploaded += worker_uploaded

                            # Do not respawn workers here; rely on initial pool.

                        except Exception as e:
                            print(f"    ✗ Worker failed for {college_name}: {e}")

                # If we exited due to stop_event, wait for remaining workers to finish
                # and aggregate their results so counts are accurate
                if active_futures:
                    done, _ = wait(active_futures)
                    for future in done:
                        try:
                            worker_crawled, worker_uploaded = future.result()
                            pages_crawled += worker_crawled
                            pages_uploaded += worker_uploaded
                        except Exception as e:
                            print(
                                f"    ✗ Worker failed during shutdown for {college_name}: {e}"
                            )
                # Ensure Playwright executor shuts down
                pw_executor.shutdown(wait=True)

                # Final progress
                if pages_crawled > 0:
                    print(
                        f"  {college_name}: {pages_crawled} pages crawled, {pages_uploaded} uploaded"
                    )

            except KeyboardInterrupt:
                print(f"\n  Interrupted crawling {college_name}")
                for future in active_futures:
                    future.cancel()
                wait(active_futures, timeout=2.0)

        print(f"\n✓ Completed crawling {college_name}")
        print(f"  Pages crawled: {pages_crawled}")
        print(f"  Pages uploaded to Milvus: {pages_uploaded}")
        print(f"  Unique URLs discovered: {len(discovered_urls)}")

        return {
            "college_name": college_name,
            "major": major,
            "base_url": base_url,
            "pages_crawled": pages_crawled,
            "pages_uploaded": pages_uploaded,
            "urls_discovered": len(discovered_urls),
            "status": "completed",
        }

    def crawl_all_colleges(
        self,
        majors_data: Dict[str, List[Dict[str, str]]],
        max_pages_per_college: int = 50,
    ):
        """
        Crawl all colleges from all majors and upload directly to Milvus.

        Args:
            majors_data: Dictionary mapping majors to college lists
            max_pages_per_college: Maximum pages to crawl per college
        """
        print("=== MULTITHREADED COLLEGE CRAWLING PIPELINE ===")
        print(f"Configuration:")
        print(f"  - Max workers per college: {self.max_workers}")
        print(f"  - Max pages per college: {max_pages_per_college}")
        print(f"  - Delay between requests: {self.delay}s")
        print(f"  - Direct upload to Milvus: ✓")

        total_colleges = sum(len(colleges) for colleges in majors_data.values())
        college_count = 0

        for major, colleges in majors_data.items():
            print(f"\n=== Processing {major.upper()} ({len(colleges)} colleges) ===")

            major_stats = {
                "total_pages_crawled": 0,
                "total_pages_uploaded": 0,
                "total_errors": 0,
            }

            for college in colleges:
                college_count += 1
                print(
                    f"\n--- [{college_count}/{total_colleges}] Processing {college['name']} ---"
                )

                try:
                    # Crawl the college site
                    college_result = self.crawl_college_site(
                        college, max_pages_per_college
                    )

                    # Update major statistics based on status
                    if college_result.get("status") == "completed":
                        major_stats["total_pages_crawled"] += college_result[
                            "pages_crawled"
                        ]
                        major_stats["total_pages_uploaded"] += college_result[
                            "pages_uploaded"
                        ]
                        # Update overall stats for crawled pages
                        with self.lock:
                            self.stats["total_pages_crawled"] += college_result[
                                "pages_crawled"
                            ]
                    elif college_result.get("status") == "blocked":
                        print(
                            f"  ⚠️  {college['name']} was blocked - skipping statistics"
                        )
                        with self.lock:
                            self.stats["total_errors"] += 1

                    with self.lock:
                        self.stats["colleges_processed"] += 1

                except Exception as e:
                    print(f"  ✗ Error processing {college['name']}: {e}")
                    with self.lock:
                        self.stats["total_errors"] += 1

            # Print major summary
            print(f"\n{major.upper()} Summary:")
            print(f"  📄 Pages crawled: {major_stats['total_pages_crawled']}")
            print(f"  📤 Pages uploaded: {major_stats['total_pages_uploaded']}")
            print(f"  ✗ Errors: {major_stats['total_errors']}")

        # Print overall summary
        print(f"\n=== FINAL CRAWLING SUMMARY ===")
        print(f"Total colleges processed: {self.stats['colleges_processed']}")
        print(f"Total pages crawled: {self.stats['total_pages_crawled']}")
        print(
            f"Total vectors uploaded to Milvus: {self.stats['total_vectors_uploaded']}"
        )
        print(
            f"Existing URLs skipped (from previous runs): {self.stats['existing_urls_skipped']}"
        )
        print(
            f"Duplicate URLs skipped (this run): {self.stats['duplicate_urls_skipped']}"
        )
        print(f"Total errors: {self.stats['total_errors']}")
        print(f"All data is now available in Milvus for vector search!")

    def run_full_crawling_pipeline(self, max_pages_per_college: int = None):
        """
        Run the complete multithreaded crawling pipeline.

        Args:
            max_pages_per_college: Maximum pages to crawl per college (uses config if None)
        """
        max_pages_per_college = max_pages_per_college or MAX_PAGES_PER_COLLEGE

        # Step 1: Read CSV files
        print("\n1. Reading CSV files...")
        majors_data = self.read_csv_files()

        if not majors_data:
            print("No college data found. Please check your CSV files.")
            return

        # Step 2: Crawl all colleges and upload to Milvus
        print("\n2. Starting multithreaded crawling and uploading to Milvus...")
        self.crawl_all_colleges(majors_data, max_pages_per_college)

        print(f"\n🎉 Multithreaded crawling completed successfully!")
        print(f"📊 All pages have been uploaded to Zilliz Cloud for vector search!")

    def get_existing_urls_for_college(self, college_name: str) -> Set[str]:
        """
        Get existing URLs for a specific college from Zilliz Cloud.

        Args:
            college_name: Name of the college to check

        Returns:
            Set of URLs that already exist for this college
        """
        existing_urls = set()

        try:
            # Load collection
            self.collection.load()

            # Query URLs for this specific college
            college_records = self.collection.query(
                expr=f'college_name == "{college_name}"',
                output_fields=["url"],
                limit=16384,
            )

            # Add URLs to set
            for record in college_records:
                url = record.get("url", "")
                if url:
                    existing_urls.add(url)

            print(f"📊 Found {len(existing_urls):,} existing URLs for {college_name}")
            return existing_urls

        except Exception as e:
            print(f"❌ Error getting existing URLs for {college_name}: {e}")
            return existing_urls

    def check_urls_batch_for_college(
        self, urls: List[str], college_name: str, batch_size: int = 100
    ) -> Set[str]:
        """
        Check a batch of URLs against existing URLs for a specific college in Zilliz Cloud.

        Args:
            urls: List of URLs to check
            college_name: Name of the college
            batch_size: Size of batches to query at once

        Returns:
            Set of URLs that already exist for this college
        """
        existing_urls = set()

        # Split URLs into batches
        for i in range(0, len(urls), batch_size):
            batch_urls = urls[i : i + batch_size]

            # Build query expression for this batch (college-specific)
            url_conditions = []
            for url in batch_urls:
                # Escape quotes in URL for Milvus query
                escaped_url = url.replace('"', '\\"')
                url_conditions.append(f'url == "{escaped_url}"')

            if not url_conditions:
                continue

            # Combine conditions with OR and add college filter
            url_expr = " || ".join(url_conditions)
            query_expr = f'college_name == "{college_name}" && ({url_expr})'

            try:
                # Query Milvus for existing URLs in this batch for this college
                existing_records = self.collection.query(
                    expr=query_expr,
                    output_fields=["url"],
                    limit=len(batch_urls),
                )

                # Add found URLs to set
                for record in existing_records:
                    url = record.get("url", "")
                    if url:
                        existing_urls.add(url)

            except Exception as e:
                print(f"❌ Error checking URL batch for {college_name}: {e}")
                continue

        return existing_urls


def main():
    """Main function to run the multithreaded crawler."""
    # Initialize crawler with config settings
    crawler = MultithreadedCollegeCrawler()

    # Run the full pipeline
    crawler.run_full_crawling_pipeline()


if __name__ == "__main__":
    main()
