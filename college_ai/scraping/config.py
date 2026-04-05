"""
Configuration file for college crawlers.
Contains all constants and settings used by the crawling system.
"""

import os
from typing import Dict, Any
from dotenv import load_dotenv

# Load environment variables from the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
env_path = os.path.join(project_root, ".env")
load_dotenv(env_path)

# ==================== CRAWLER SETTINGS ====================

# Request settings (provide safe defaults for 2 vCPU instances)
CRAWLER_DELAY = float(
    os.getenv("CRAWLER_DELAY", "1.0")
)  # Delay between requests in seconds
CRAWLER_MAX_WORKERS = int(
    os.getenv("CRAWLER_MAX_WORKERS", "6")
)  # Number of worker threads per college (PW-heavy default)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))  # Request timeout in seconds
REQUEST_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

# Proxy settings (comma-separated list of proxy URLs, e.g., "http://user:pass@ip:port, http://ip2:port")
RAW_HTTP_PROXIES = os.getenv("CRAWLER_PROXIES", "")
HTTP_PROXIES = [p.strip() for p in RAW_HTTP_PROXIES.split(",") if p.strip()]
USE_CURL_CFFI = os.getenv("USE_CURL_CFFI", "1") == "1"

# Camoufox toggle (Firefox-based stealth browser for Playwright path)
USE_CAMOUFOX = os.getenv("USE_CAMOUFOX", "1") == "1"

# Crawling limits
MAX_PAGES_PER_COLLEGE = int(
    os.getenv("MAX_PAGES_PER_COLLEGE", "500")
)  # Maximum pages to crawl per college
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "3"))  # Maximum crawl depth from starting URL
MAX_RETRIES = int(
    os.getenv("MAX_RETRIES", "3")
)  # Maximum retry attempts for failed requests

# Per-college crawl time budget (seconds)
MAX_CRAWL_TIME_PER_COLLEGE = int(os.getenv("MAX_CRAWL_TIME_PER_COLLEGE", "300"))

# Inter-college parallelism: crawl multiple colleges simultaneously (different domains)
INTER_COLLEGE_PARALLELISM = int(os.getenv("INTER_COLLEGE_PARALLELISM", "4"))

# Milvus insert buffer: batch inserts to reduce lock contention
MILVUS_INSERT_BUFFER_SIZE = int(os.getenv("MILVUS_INSERT_BUFFER_SIZE", "50"))
MILVUS_INSERT_FLUSH_INTERVAL = float(os.getenv("MILVUS_INSERT_FLUSH_INTERVAL", "2.0"))

# Delta crawling: skip unchanged pages on subsequent runs via HTTP conditional
# headers (ETag/Last-Modified) and content hashing
ENABLE_DELTA_CRAWLING = os.getenv("ENABLE_DELTA_CRAWLING", "1") == "1"

# Playwright pool: pre-launch persistent browser contexts for faster JS rendering
PLAYWRIGHT_POOL_SIZE = int(os.getenv("PLAYWRIGHT_POOL_SIZE", "5"))
PLAYWRIGHT_POOL_ROTATE_AFTER = int(os.getenv("PLAYWRIGHT_POOL_ROTATE_AFTER", "50"))

# Content filtering
MIN_CONTENT_LENGTH = int(
    os.getenv("MIN_CONTENT_LENGTH", "100")
)  # Minimum content length to consider valid
MAX_CONTENT_LENGTH = int(
    os.getenv("MAX_CONTENT_LENGTH", "50000")
)  # Maximum content length for storage
MAX_TITLE_LENGTH = int(
    os.getenv("MAX_TITLE_LENGTH", "500")
)  # Maximum title length for storage

# URL filtering
SKIP_EXTENSIONS = [
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
    ".rar",
    ".tar",
    ".gz",
    ".7z",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".svg",
    ".webp",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".css",
    ".js",
    ".xml",
    ".json",
    ".rss",
    ".atom",
    ".exe",
    ".msi",
    ".dmg",
    ".pkg",
    ".deb",
    ".rpm",
]

SKIP_PATHS = [
    "/admin",
    "/login",
    "/logout",
    "/register",
    "/signup",
    "/api/",
    "/ajax/",
    "/json/",
    "/xml/",
    "/search",
    "/filter",
    "/sort",
    "/print",
    "/download",
    "/export",
    "/calendar",
    "/events",
    "/news/archive",
    "/contact",
    "/feedback",
    "/survey",
]

# ==================== ZILLIZ CLOUD SETTINGS ====================

# Zilliz Cloud connection
ZILLIZ_URI = os.getenv("ZILLIZ_URI")  # e.g., "https://your-cluster.zillizcloud.com"
ZILLIZ_API_KEY = os.getenv("ZILLIZ_API_KEY")  # Your Zilliz API key
ZILLIZ_COLLECTION_NAME = os.getenv("ZILLIZ_COLLECTION_NAME", "colleges")

# Vector settings
VECTOR_DIM = 1536  # Matches OpenAI embedding dimension

# Chunking settings
CHUNK_MAX_TOKENS = int(os.getenv("CHUNK_MAX_TOKENS", "512"))
CHUNK_OVERLAP_TOKENS = int(os.getenv("CHUNK_OVERLAP_TOKENS", "50"))

# Contextual prefixes: prepend LLM-generated context to each chunk before embedding.
# Improves retrieval accuracy ~35% (Anthropic) but adds an LLM call per chunk during crawl.
# Off by default — set CONTEXTUAL_PREFIXES=1 to enable.
CONTEXTUAL_PREFIXES = os.getenv("CONTEXTUAL_PREFIXES", "0") == "1"

# ==================== PAGE TYPE CLASSIFICATION ====================

# URL pattern → page type mapping for the page_type Milvus field.
# Used by the crawler to classify pages at insert time.
#
# Pattern format: [/.]segment(/|$|\?|\.) — matches path segments (/transfer/),
# subdomains (transfer.asu.edu), leaf URLs (/transfer), and query strings (/transfer?x=1).
#
# ORDER MATTERS: first match wins. More specific types (transfer, international,
# diversity) are listed before broader ones (admissions, academics) to prevent
# mis-classification of compound paths like /diversity/programs.
PAGE_TYPE_PATTERNS = {
    # --- Specific types first (would otherwise be swallowed by broader categories) ---
    "transfer": [
        r"[/.]transfer(/|$|\?|\.)",
        r"[/.]transferring(/|$|\?|\.)",
        r"[/.]credit-transfer(/|$|\?|\.)",
        r"[/.]articulation(/|$|\?|\.)",
        r"[/.]advanced-standing(/|$|\?|\.)",
    ],
    "international": [
        r"[/.]international(/|$|\?|\.)",
        r"[/.]global-education(/|$|\?|\.)",
        r"[/.]study-abroad(/|$|\?|\.)",
        r"[/.]visa(/|$|\?|\.)",
        r"[/.]immigration(/|$|\?|\.)",
        r"[/.]toefl(/|$|\?|\.)",
        r"[/.]ielts(/|$|\?|\.)",
        r"[/.]english-proficiency(/|$|\?|\.)",
    ],
    # --- Core categories (diversity before academics to win on /diversity/programs) ---
    "admissions": [
        r"[/.]admissions?(/|$|\?|\.)",
        r"[/.]apply(/|$|\?|\.)",
        r"[/.]application(/|$|\?|\.)",
        r"[/.]enrollment(/|$|\?|\.)",
        r"[/.]admitted(/|$|\?|\.)",
        r"[/.]freshman(/|$|\?|\.)",
        r"[/.]first-year(/|$|\?|\.)",
        r"[/.]prospective(/|$|\?|\.)",
        r"[/.]early-decision(/|$|\?|\.)",
        r"[/.]early-action(/|$|\?|\.)",
        r"[/.]acceptance(/|$|\?|\.)",
        r"[/.]accepted-students(/|$|\?|\.)",
    ],
    "diversity": [
        r"[/.]diversity(/|$|\?|\.)",
        r"[/.]inclusion(/|$|\?|\.)",
        r"[/.]multicultural(/|$|\?|\.)",
        r"[/.]equity(/|$|\?|\.)",
        r"[/.]belonging(/|$|\?|\.)",
        r"[/.]dei(/|$|\?|\.)",
    ],
    "academics": [
        r"[/.]academics?(/|$|\?|\.)",
        r"[/.]programs?(/|$|\?|\.)",
        r"[/.]majors?(/|$|\?|\.)",
        r"[/.]minors?(/|$|\?|\.)",
        r"[/.]degrees?(/|$|\?|\.)",
        r"[/.]courses?(/|$|\?|\.)",
        r"[/.]curriculum(/|$|\?|\.)",
        r"[/.]departments?(/|$|\?|\.)",
        r"[/.]catalog(/|$|\?|\.)",
        r"[/.]registrar(/|$|\?|\.)",
        r"[/.]honors(/|$|\?|\.)",
        r"[/.]undergraduate-programs?(/|$|\?|\.)",
    ],
    "financial_aid": [
        r"[/.]financial-?aid(/|$|\?|\.)",
        r"[/.]tuition(/|$|\?|\.)",
        r"[/.]scholarships?(/|$|\?|\.)",
        r"[/.]cost-of-attendance(/|$|\?|\.)",
        r"[/.]net-price(/|$|\?|\.)",
        r"[/.]fafsa(/|$|\?|\.)",
        r"[/.]work-study(/|$|\?|\.)",
        r"[/.]bursar(/|$|\?|\.)",
        r"[/.]billing(/|$|\?|\.)",
        r"[/.]affordability(/|$|\?|\.)",
    ],
    "outcomes": [
        r"[/.]career-services?(/|$|\?|\.)",
        r"[/.]career-center(/|$|\?|\.)",
        r"[/.]careers?(/|$|\?|\.)",
        r"[/.]outcomes?(/|$|\?|\.)",
        r"[/.]employment(/|$|\?|\.)",
        r"[/.]placement(/|$|\?|\.)",
        r"[/.]internships?(/|$|\?|\.)",
        r"[/.]post-graduation(/|$|\?|\.)",
        r"[/.]alumni-outcomes(/|$|\?|\.)",
    ],
    "safety_health": [
        r"[/.]campus-safety(/|$|\?|\.)",
        r"[/.]campus-police(/|$|\?|\.)",
        r"[/.]safety(/|$|\?|\.)",
        r"[/.]police(/|$|\?|\.)",
        r"[/.]security(/|$|\?|\.)",
        r"[/.]health-services?(/|$|\?|\.)",
        r"[/.]counseling(/|$|\?|\.)",
        r"[/.]disability(/|$|\?|\.)",
        r"[/.]mental-health(/|$|\?|\.)",
        r"[/.]wellness(/|$|\?|\.)",
        r"[/.]clery(/|$|\?|\.)",
        r"[/.]accessibility(/|$|\?|\.)",
    ],
    # --- Broad categories last ---
    "about": [
        r"[/.]about(/|$|\?|\.)",
        r"[/.]overview(/|$|\?|\.)",
        r"[/.]mission(/|$|\?|\.)",
        r"[/.]history(/|$|\?|\.)",
        r"[/.]fast-facts(/|$|\?|\.)",
        r"[/.]facts-and-figures(/|$|\?|\.)",
        r"[/.]at-a-glance(/|$|\?|\.)",
        r"[/.]rankings(/|$|\?|\.)",
        r"[/.]accreditation(/|$|\?|\.)",
        r"[/.]president(/|$|\?|\.)",
        r"[/.]leadership(/|$|\?|\.)",
        r"[/.]who-we-are(/|$|\?|\.)",
    ],
    "campus_life": [
        r"[/.]campus(/|$|\?|\.)",
        r"[/.]housing(/|$|\?|\.)",
        r"[/.]dining(/|$|\?|\.)",
        r"[/.]student-life(/|$|\?|\.)",
        r"[/.]residence(/|$|\?|\.)",
        r"[/.]dorms?(/|$|\?|\.)",
        r"[/.]athletics(/|$|\?|\.)",
        r"[/.]clubs?(/|$|\?|\.)",
        r"[/.]greek(/|$|\?|\.)",
        r"[/.]recreation(/|$|\?|\.)",
        r"[/.]student-activities(/|$|\?|\.)",
        r"[/.]student-organizations?(/|$|\?|\.)",
        r"[/.]fraternity(/|$|\?|\.)",
        r"[/.]sorority(/|$|\?|\.)",
        r"[/.]sports(/|$|\?|\.)",
    ],
    "research": [
        r"[/.]research(/|$|\?|\.)",
        r"[/.]faculty(/|$|\?|\.)",
        r"[/.]laboratories?(/|$|\?|\.)",
        r"[/.]institute(/|$|\?|\.)",
        r"[/.]publications?(/|$|\?|\.)",
    ],
}

# ==================== OPENAI SETTINGS ====================

# OpenAI API settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ==================== FILE PATHS ====================

# Directory paths
BASE_DIR = project_root
COLLEGES_DIR = os.path.join(os.path.dirname(__file__), "colleges")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")

# Ensure directories exist
os.makedirs(COLLEGES_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# ==================== LOGGING SETTINGS ====================

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_FILE = os.path.join(LOGS_DIR, "crawler.log")

# ==================== URL PATTERNS ====================

# URL patterns to exclude during crawling
EXCLUDED_URL_PATTERNS = [
    r".*\.pdf$",
    r".*\.doc[x]?$",
    r".*\.xls[x]?$",
    r".*\.ppt[x]?$",
    r".*\.zip$",
    r".*\.rar$",
    r".*\.exe$",
    r".*\.dmg$",
    r".*\.(jpg|jpeg|png|gif|bmp|svg|ico)$",
    r".*\.(mp3|mp4|avi|mov|wmv|flv)$",
    r".*/calendar/.*",
    r".*/events?/.*",
    r".*/news/.*",
    r".*/blog/.*",
    r".*/login.*",
    r".*/register.*",
    r".*/admin.*",
    r".*/wp-admin.*",
    r".*/wp-content.*",
    r".*facebook\.com.*",
    r".*twitter\.com.*",
    r".*instagram\.com.*",
    r".*linkedin\.com.*",
    r".*youtube\.com.*",
    r"mailto:.*",
]

# URL patterns to prioritize during crawling
PRIORITY_URL_PATTERNS = [
    r".*/academics?/.*",
    r".*/programs?/.*",
    r".*/majors?/.*",
    r".*/degrees?/.*",
    r".*/courses?/.*",
    r".*/curriculum/.*",
    r".*/departments?/.*",
    r".*/schools?/.*",
    r".*/colleges?/.*",
    r".*/admissions?/.*",
    r".*/apply/.*",
    r".*/requirements/.*",
    r".*/tuition/.*",
    r".*/financial-aid/.*",
    r".*/scholarships?/.*",
    r".*/about/.*",
    r".*/overview/.*",
]
# ==================== VALIDATION SETTINGS ====================

# Content validation settings
MIN_WORDS_PER_PAGE = 50  # Minimum words to consider page valid
VALID_CONTENT_TYPES = ["text/html", "application/xhtml+xml"]

# Playwright fallback settings
USE_PLAYWRIGHT_FALLBACK = os.getenv("USE_PLAYWRIGHT_FALLBACK", "1") == "1"
PLAYWRIGHT_MAX_CONCURRENCY = int(os.getenv("PLAYWRIGHT_MAX_CONCURRENCY", "3"))
PLAYWRIGHT_NAV_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_NAV_TIMEOUT_MS", "15000"))
PLAYWRIGHT_AGGRESSIVE_FALLBACK = os.getenv("PLAYWRIGHT_AGGRESSIVE_FALLBACK", "0") == "1"

# Queue management settings for better Playwright compatibility
QUEUE_TIMEOUT_SECONDS = float(
    os.getenv("QUEUE_TIMEOUT_SECONDS", "1.5")
)  # 1.5s per check — fast enough to notice new work, short enough to not waste time
MAX_EMPTY_CHECKS = int(
    os.getenv("MAX_EMPTY_CHECKS", "8")
)  # ~12s idle wait; Playwright patience handled separately via bounded reset
PLAYWRIGHT_COOKIE_PERSISTENCE = (
    os.getenv("PLAYWRIGHT_COOKIE_PERSISTENCE", "1") == "1"
)  # Enable cookie persistence
PLAYWRIGHT_ENHANCED_ANTI_DETECTION = (
    os.getenv("PLAYWRIGHT_ENHANCED_ANTI_DETECTION", "1") == "1"
)  # Enhanced anti-detection measures
PLAYWRIGHT_RETRY_ATTEMPTS = int(
    os.getenv("PLAYWRIGHT_RETRY_ATTEMPTS", "2")
)  # Number of retry attempts
PLAYWRIGHT_REDIRECT_EXTRA_WAIT = int(
    os.getenv("PLAYWRIGHT_REDIRECT_EXTRA_WAIT", "5000")
)  # Extra wait time (ms) for redirected pages
PLAYWRIGHT_REDIRECT_DETECTION = (
    os.getenv("PLAYWRIGHT_REDIRECT_DETECTION", "1") == "1"
)  # Enable redirect detection and handling

# Playwright resource blocking: abort these resource types to speed up page loads.
# Does NOT block document, script, xhr, fetch — those are needed for SPA content.
PLAYWRIGHT_BLOCKED_RESOURCE_TYPES = {
    "image", "imageset", "stylesheet", "font", "media",
    "texttrack", "object", "beacon", "csp_report", "manifest",
}

# Playwright URL-pattern blocking: abort requests to these analytics/tracking domains.
PLAYWRIGHT_BLOCKED_URL_PATTERNS = [
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "hotjar.com", "cdn.segment.com", "sentry.io",
    "amplitude.com", "newrelic.com", "optimizely.com", "adservice.google.com",
    "pagead2.googlesyndication.com", "connect.facebook.net",
]

# ==================== HELPER FUNCTIONS ====================


def get_config_dict() -> Dict[str, Any]:
    """
    Get all configuration as a dictionary.

    Returns:
        Dictionary containing all configuration values
    """
    config = {}

    # Get all module variables that are uppercase (constants)
    for key, value in globals().items():
        if key.isupper() and not key.startswith("_"):
            config[key] = value

    return config


def validate_config() -> bool:
    """
    Validate that all required configuration values are set.

    Returns:
        True if configuration is valid, False otherwise
    """
    # Check for Zilliz Cloud connection
    required_vars = [
        "ZILLIZ_URI",
        "ZILLIZ_API_KEY",
        "ZILLIZ_COLLECTION_NAME",
        "VECTOR_DIM",
    ]

    for var in required_vars:
        if not globals().get(var):
            print(f"Error: Required configuration variable {var} is not set")
            return False

    print("✅ Using Zilliz Cloud connection")

    # Check if OpenAI API key is set
    if not OPENAI_API_KEY:
        print(
            "Warning: OPENAI_API_KEY is not set. Embedding functionality will not work."
        )

    return True


# Validate configuration on import
if __name__ != "__main__":
    validate_config()
