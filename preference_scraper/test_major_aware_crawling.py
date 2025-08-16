#!/usr/bin/env python3
"""
Test script to demonstrate the new major-aware crawling logic.
This script shows how the crawler now handles URLs that exist but don't have the current major.
"""

import os
import sys

# Add the crawlers directory to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "crawlers")))

from multithreaded_crawler import MultithreadedCollegeCrawler
from config import *


def test_major_aware_logic():
    """Test the new major-aware canonical URL logic."""

    print("=== Testing Major-Aware Crawling Logic ===\n")

    # Initialize the crawler
    crawler = MultithreadedCollegeCrawler()

    # Test the new helper function
    print("Testing _check_url_has_major function:")

    # Example test cases
    test_cases = [
        {
            "url": "https://example.edu/computer-science",
            "major": "computer_science",
            "description": "CS page with CS major",
        },
        {
            "url": "https://example.edu/computer-science",
            "major": "business",
            "description": "CS page with business major (should return False)",
        },
    ]

    for test_case in test_cases:
        url = test_case["url"]
        major = test_case["major"]
        description = test_case["description"]

        try:
            canon_key = crawler._url_canonical_key(url)
            has_major = crawler._check_url_has_major(canon_key, major)
            print(f"  {description}: {has_major}")
        except Exception as e:
            print(f"  {description}: Error - {e}")

    print("\n=== Major-Aware Logic Summary ===")
    print("✅ New behavior:")
    print("  - URLs that exist but don't have current major: CONTINUE crawling")
    print("  - URLs that exist with current major: SKIP crawling")
    print("  - Links from pages without current major: ADD to queue")
    print("  - Links from pages with current major: SKIP adding to queue")
    print("\n🔄 Process:")
    print("  1. Check if URL exists in canonical URLs set")
    print("  2. If exists, check if it has the current major")
    print("  3. Only skip if URL exists AND has current major")
    print(
        "  4. Otherwise, continue crawling and let upload_to_milvus handle major updates"
    )
    print("\n📊 Benefits:")
    print("  - Better coverage across majors")
    print("  - No duplicate crawling of same content")
    print("  - Efficient major addition to existing records")
    print("  - Maintains BFS link discovery for new majors")


def test_configuration():
    """Test and display the current configuration."""

    print("=== Configuration ===")
    print(f"USE_PLAYWRIGHT_FALLBACK: {USE_PLAYWRIGHT_FALLBACK}")
    print(f"PLAYWRIGHT_AGGRESSIVE_FALLBACK: {PLAYWRIGHT_AGGRESSIVE_FALLBACK}")
    print(f"MAX_PAGES_PER_COLLEGE: {MAX_PAGES_PER_COLLEGE}")
    print(f"CRAWLER_MAX_WORKERS: {CRAWLER_MAX_WORKERS}")
    print()


if __name__ == "__main__":
    test_configuration()
    test_major_aware_logic()
