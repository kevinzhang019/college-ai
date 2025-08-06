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
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from typing import Dict, List, Any, Optional, Set
from datetime import datetime
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from pymilvus import (
    connections,
    Collection,
    FieldSchema,
    CollectionSchema,
    DataType,
    utility,
)
import openai

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from preference_scraper.utils.openai_embed import get_embedding
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
        self.max_retries = 3
        self.robots_cache = {}  # Cache robots.txt results

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

        # Milvus connection
        self.connect_milvus()
        self.collection = self.get_or_create_collection()

        # Initialize existing URLs as empty - will be populated per college
        self.existing_urls = set()

        # Crawling statistics
        self.stats = {
            "total_pages_crawled": 0,
            "total_pages_uploaded": 0,
            "total_errors": 0,
            "colleges_processed": 0,
            "duplicate_urls_skipped": 0,
            "existing_urls_skipped": 0,
        }

    def rotate_user_agent(self):
        """Rotate User-Agent to avoid detection."""
        new_user_agent = random.choice(self.user_agents)
        self.session.headers.update({"User-Agent": new_user_agent})
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
            FieldSchema(name="major", dtype=DataType.VARCHAR, max_length=64),
        ]

        schema = CollectionSchema(fields, description="College pages with embeddings")

        if utility.has_collection(collection_name):
            existing = Collection(collection_name)
            try:
                embed_field = next(
                    f for f in existing.schema.fields if f.name == "embedding"
                )
                if embed_field.params.get("dim") != VECTOR_DIM:
                    print(
                        f"⚠️ Existing collection embedding dim {embed_field.params.get('dim')} != {VECTOR_DIM}. Recreating collection ..."
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
                absolute_url = urljoin(base_url, href)

                # Normalize URL by removing fragments and trailing slashes
                parsed = urlparse(absolute_url)
                normalized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if parsed.query:
                    normalized_url += f"?{parsed.query}"

                # Remove trailing slash unless it's the root path
                if normalized_url.endswith("/") and len(parsed.path) > 1:
                    normalized_url = normalized_url.rstrip("/")

                # Check if it's an internal link
                if self.is_internal_link(normalized_url, base_url):
                    links.add(normalized_url)

            except Exception as e:
                print(f"    Warning: Error processing link '{href}': {e}")
                continue

        return list(links)

    def scrape_page(
        self, url: str, session: requests.Session = None
    ) -> Optional[Dict[str, Any]]:
        """Scrape a single page and return structured data."""
        try:
            print(f"    Crawling: {url}")

            # Add small delay between requests to be respectful
            time.sleep(random.uniform(self.min_delay, self.max_delay))

            # Use provided session or fall back to shared session
            request_session = session or self.session

            # Fetch the page with retry logic for 403 errors
            response = None
            for attempt in range(self.max_retries):
                try:
                    response = request_session.get(url, timeout=REQUEST_TIMEOUT)

                    # Handle 403 errors specifically
                    if response.status_code == 403:
                        print(
                            f"    ⚠️  403 Forbidden for {url} (attempt {attempt + 1}/{self.max_retries})"
                        )
                        if attempt < self.max_retries - 1:
                            # Rotate User-Agent and wait longer for 403 errors before retry
                            new_ua = self.rotate_user_agent()
                            print(f"    🔄 Rotated User-Agent to: {new_ua[:50]}...")
                            time.sleep(self.delay * (attempt + 1) * 2)
                            continue
                        else:
                            print(
                                f"    ✗ Giving up on {url} after {self.max_retries} attempts due to 403"
                            )
                            return None

                    response.raise_for_status()
                    break

                except requests.exceptions.HTTPError as e:
                    if "403" in str(e):
                        print(
                            f"    ⚠️  403 Forbidden for {url} (attempt {attempt + 1}/{self.max_retries})"
                        )
                        if attempt < self.max_retries - 1:
                            # Rotate User-Agent and wait longer for 403 errors before retry
                            new_ua = self.rotate_user_agent()
                            print(f"    🔄 Rotated User-Agent to: {new_ua[:50]}...")
                            time.sleep(self.delay * (attempt + 1) * 2)
                            continue
                        else:
                            print(
                                f"    ✗ Giving up on {url} after {self.max_retries} attempts due to 403"
                            )
                            return None
                    else:
                        raise e
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise e
                    time.sleep(self.delay * (attempt + 1))
                    continue

            if not response:
                return None

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

            # Check if we have meaningful content
            if len(cleaned_content.strip()) < 50:
                print(
                    f"    ⚠️  Very little content found for {url} ({len(cleaned_content)} chars)"
                )

            # Extract internal links
            internal_links = self.extract_internal_links(soup, url)

            # Debug info for stuck crawler
            if len(internal_links) > 0:
                print(f"    🔗 Found {len(internal_links)} internal links for {url}")

            return {
                "url": url,
                "title": title_text,
                "content": cleaned_content,
                "internal_links": internal_links,
                "word_count": len(cleaned_content.split()),
                "crawled_at": datetime.now().isoformat(),
            }

        except Exception as e:
            print(f"    ✗ Error scraping {url}: {e}")
            return None

    def upload_to_milvus(
        self, page_data: Dict[str, Any], college_name: str, major: str
    ) -> bool:
        """Upload a single page to Milvus with embedding."""
        try:
            # Check if URL already exists in Milvus from previous runs
            with self.lock:
                if page_data["url"] in self.existing_urls:
                    print(
                        f"    ⚠️  Skipping existing URL from previous run: {page_data['url']}"
                    )
                    self.stats["existing_urls_skipped"] += 1
                    return False

                # Check if URL has already been uploaded in this run
                if page_data["url"] in self.uploaded_urls:
                    print(
                        f"    ⚠️  Skipping duplicate URL from this run: {page_data['url']}"
                    )
                    self.stats["duplicate_urls_skipped"] += 1
                    return False

                # Add to uploaded URLs set
                self.uploaded_urls.add(page_data["url"])

            # Generate embedding for the content
            content_for_embedding = f"{page_data['title']} {page_data['content']}"
            embedding = get_embedding(content_for_embedding)

            if not embedding:
                print(f"    ✗ Failed to generate embedding for {page_data['url']}")
                return False

            # Prepare data for Milvus matching the new schema
            import time

            current_timestamp = int(time.time())

            # Create a combined description from title and content
            description = f"{page_data['title']} {page_data['content']}"
            if len(description) > 2047:  # Leave room for null terminator
                description = description[:2047]

            data = [
                {
                    "id": str(uuid.uuid4()),
                    "college_name": college_name,
                    "url": page_data["url"],
                    "title": page_data["title"][: MAX_TITLE_LENGTH - 1],
                    "content": page_data["content"][: MAX_CONTENT_LENGTH - 1],
                    "embedding": embedding,
                    "crawled_at": page_data["crawled_at"],
                    "major": major,
                }
            ]

            # Insert into Milvus
            self.collection.insert(data)

            with self.lock:
                self.stats["total_pages_uploaded"] += 1

            print(f"    ✓ Uploaded to Milvus: {page_data['url']}")
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

        # Get existing URLs for this college to prevent duplicates
        self.existing_urls = self.get_existing_urls_for_college(college_name)

        # Reset state for this college
        crawled_urls = set()
        discovered_urls = set()

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
        url_queue = [(0, normalized_base)]
        discovered_urls.add(normalized_base)

        pages_crawled = 0
        pages_uploaded = 0

        # Use ThreadPoolExecutor with true work-stealing
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Use a real queue for thread-safe operations
            from queue import Queue

            work_queue = Queue()
            work_queue.put((0, normalized_base))

            # Track active futures
            active_futures = set()

            def worker_task():
                """Efficient worker for work-stealing BFS"""
                local_crawled = 0
                local_uploaded = 0
                consecutive_empty_checks = 0
                max_empty_checks = 10  # Exit if queue is empty for too long

                # Create thread-local session for thread safety
                worker_session = requests.Session()
                worker_session.headers.update(self.session.headers)

                while local_crawled < max_pages:
                    try:
                        # Get next URL with timeout
                        depth, url = work_queue.get(timeout=1.0)  # Increased timeout
                        consecutive_empty_checks = 0  # Reset counter

                        # Skip if already crawled
                        if url in crawled_urls:
                            continue

                        # Mark as crawled
                        crawled_urls.add(url)

                        # Scrape page with thread-local session
                        page_data = self.scrape_page(url, worker_session)
                        if not page_data:
                            continue

                        local_crawled += 1

                        # Upload to Milvus
                        if self.upload_to_milvus(page_data, college_name, major):
                            local_uploaded += 1

                        # Add new links to queue (BFS)
                        new_links = page_data.get("internal_links", [])
                        new_depth = depth + 1
                        links_added = 0

                        # Filter out links that already exist in Milvus
                        filtered_links = []
                        for link in new_links:
                            if (
                                link not in crawled_urls
                                and link not in discovered_urls
                                and link
                                not in self.existing_urls  # Skip URLs from previous runs
                                and len(crawled_urls) < max_pages
                            ):
                                filtered_links.append(link)

                        # Add filtered links to queue
                        for link in filtered_links:
                            discovered_urls.add(link)
                            work_queue.put((new_depth, link))
                            links_added += 1

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
                        if consecutive_empty_checks >= max_empty_checks:
                            print(
                                f"    ⏭️  Exiting worker for {college_name} - queue empty for too long"
                            )
                            break

                        # Check if we should exit
                        if len(crawled_urls) >= max_pages:
                            break
                        continue
                    except Exception as e:
                        print(f"    ✗ Worker error for {college_name}: {e}")
                        continue

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
                max_crawl_time = 300  # 5 minutes max per college

                while active_futures and len(crawled_urls) < max_pages:
                    # Check for timeout
                    if time.time() - start_time > max_crawl_time:
                        print(
                            f"    ⏰ Timeout reached for {college_name} ({max_crawl_time}s)"
                        )
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
                            with self.lock:
                                pages_crawled += worker_crawled
                                pages_uploaded += worker_uploaded

                            # Add new worker if we still have work
                            if not work_queue.empty() and len(crawled_urls) < max_pages:
                                new_future = executor.submit(worker_task)
                                active_futures.add(new_future)

                        except Exception as e:
                            print(f"    ✗ Worker failed for {college_name}: {e}")

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
        print(f"Total pages uploaded to Milvus: {self.stats['total_pages_uploaded']}")
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
