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

# Request settings
CRAWLER_DELAY = float(os.getenv("CRAWLER_DELAY"))  # Delay between requests in seconds
CRAWLER_MAX_WORKERS = int(
    os.getenv("CRAWLER_MAX_WORKERS")
)  # Number of worker threads per college
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT"))  # Request timeout in seconds
REQUEST_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

# Crawling limits
MAX_PAGES_PER_COLLEGE = int(
    os.getenv("MAX_PAGES_PER_COLLEGE")
)  # Maximum pages to crawl per college
MAX_DEPTH = int(os.getenv("MAX_DEPTH"))  # Maximum crawl depth from starting URL
MAX_RETRIES = int(
    os.getenv("MAX_RETRIES")
)  # Maximum retry attempts for failed requests

# Content filtering
MIN_CONTENT_LENGTH = int(
    os.getenv("MIN_CONTENT_LENGTH")
)  # Minimum content length to consider valid
MAX_CONTENT_LENGTH = int(
    os.getenv("MAX_CONTENT_LENGTH")
)  # Maximum content length for storage
MAX_TITLE_LENGTH = int(
    os.getenv("MAX_TITLE_LENGTH")
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
ZILLIZ_COLLECTION_NAME = os.getenv("ZILLIZ_COLLECTION_NAME", "college_pages")

# If MILVUS_HOST looks like a Zilliz URI, use it as Zilliz URI
if (
    os.getenv("MILVUS_HOST")
    and os.getenv("MILVUS_HOST").startswith("https://")
    and "zilliz" in os.getenv("MILVUS_HOST")
):
    ZILLIZ_URI = os.getenv("MILVUS_HOST")

# Vector settings
VECTOR_DIM = 1536  # Matches OpenAI embedding dimension
INDEX_TYPE = "IVF_FLAT"
METRIC_TYPE = "L2"

# ==================== OPENAI SETTINGS ====================

# OpenAI API settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_EMBEDDING_MODEL = "text-embedding-ada-002"
OPENAI_MAX_RETRIES = 3
OPENAI_RATE_LIMIT_DELAY = 1.0  # Delay between API calls

# ==================== FILE PATHS ====================

# Directory paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

# ==================== MAJOR CATEGORIES ====================

# Major categories for classification
MAJOR_CATEGORIES = {
    "business": [
        "business",
        "management",
        "finance",
        "accounting",
        "marketing",
        "economics",
        "entrepreneurship",
        "mba",
        "commerce",
    ],
    "engineering": [
        "engineering",
        "computer science",
        "software",
        "electrical",
        "mechanical",
        "civil",
        "chemical",
        "aerospace",
        "biomedical",
    ],
    "healthcare": [
        "medicine",
        "nursing",
        "pharmacy",
        "dentistry",
        "veterinary",
        "health",
        "medical",
        "therapy",
        "rehabilitation",
    ],
    "liberal_arts": [
        "liberal arts",
        "humanities",
        "english",
        "literature",
        "history",
        "philosophy",
        "art",
        "music",
        "theater",
        "languages",
    ],
    "science": [
        "biology",
        "chemistry",
        "physics",
        "mathematics",
        "statistics",
        "environmental",
        "geology",
        "astronomy",
        "research",
    ],
    "social_sciences": [
        "psychology",
        "sociology",
        "political science",
        "anthropology",
        "social work",
        "criminal justice",
        "international relations",
    ],
    "education": [
        "education",
        "teaching",
        "elementary",
        "secondary",
        "special education",
        "curriculum",
        "instruction",
    ],
}

# ==================== VALIDATION SETTINGS ====================

# Content validation settings
MIN_WORDS_PER_PAGE = 50  # Minimum words to consider page valid
MAX_DUPLICATE_THRESHOLD = 0.8  # Similarity threshold for duplicate detection
VALID_CONTENT_TYPES = ["text/html", "application/xhtml+xml"]

# ==================== EXPORT SETTINGS ====================

# Export and backup settings
EXPORT_BATCH_SIZE = 1000  # Number of records to export at once
BACKUP_ENABLED = True
BACKUP_INTERVAL_HOURS = 24  # Backup interval in hours

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
