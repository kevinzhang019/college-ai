#!/usr/bin/env python3
"""
Test script to demonstrate the improved cookie handling functionality.
This script tests cookie persistence and banner acceptance.
"""

import os
import sys
import time

# Add the crawlers directory to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "crawlers")))

from multithreaded_crawler import MultithreadedCollegeCrawler
from config import *


def test_cookie_handling():
    """Test the improved cookie handling functionality."""

    print("=== Testing Improved Cookie Handling ===\n")

    # Initialize the crawler
    crawler = MultithreadedCollegeCrawler()

    # Test URLs that are likely to have cookie banners
    test_urls = [
        # Sites with common cookie banners
        "https://www.bbc.com/",
        "https://www.nytimes.com/",
        "https://www.theguardian.com/",
        "https://www.economist.com/",
        # University sites that might have cookie banners
        "https://www.harvard.edu/",
        "https://www.stanford.edu/",
        "https://www.mit.edu/",
    ]

    print("Testing cookie handling with various URLs:")
    print(f"Cookie persistence enabled: {crawler.playwright_cookie_persistence}")
    print(f"Cookie storage directory: {crawler.cookie_storage_dir}")
    print()

    for i, url in enumerate(test_urls, 1):
        print(f"Test {i}: {url}")
        print("-" * 50)

        try:
            # Test Playwright scraping with cookie handling
            print("Testing Playwright with cookie handling...")
            start_time = time.time()

            result = crawler._scrape_with_playwright(url)
            elapsed_time = time.time() - start_time

            if result:
                print(f"   ✓ Playwright scraping successful")
                print(f"   - Title: {result.get('title', 'N/A')[:50]}...")
                print(f"   - Content length: {len(result.get('content', ''))}")
                print(f"   - Word count: {result.get('word_count', 0)}")
                print(f"   - Internal links: {len(result.get('internal_links', []))}")
                print(f"   - Time: {elapsed_time:.2f}s")

                # Check if cookies were saved
                try:
                    netloc = url.split("//")[1].split("/")[0]
                    cookie_path = crawler._get_cookie_storage_path(netloc)
                    if os.path.exists(cookie_path):
                        print(f"   🍪 Cookies saved for {netloc}")
                        # Show cookie file size
                        size = os.path.getsize(cookie_path)
                        print(f"   📁 Cookie file size: {size} bytes")
                    else:
                        print(f"   ⚠️  No cookies saved for {netloc}")
                except Exception as e:
                    print(f"   ⚠️  Could not check cookie file: {e}")
            else:
                print(f"   ✗ Playwright scraping failed")

        except Exception as e:
            print(f"   ✗ Error testing {url}: {e}")

        print("\n" + "=" * 60 + "\n")
        time.sleep(2)  # Be respectful to servers

    print("=== Cookie Handling Test Complete ===")


def test_cookie_functions():
    """Test the individual cookie handling functions."""

    print("=== Testing Cookie Functions ===\n")

    crawler = MultithreadedCollegeCrawler()

    # Test cookie storage path generation
    test_netlocs = [
        "example.com",
        "www.bbc.com",
        "sub.domain.co.uk",
        "site-with-special-chars.com",
    ]

    print("Testing cookie storage path generation:")
    for netloc in test_netlocs:
        path = crawler._get_cookie_storage_path(netloc)
        print(f"  {netloc} -> {os.path.basename(path)}")

    print("\nTesting cookie loading/saving:")

    # Test with a sample storage state
    test_storage_state = {
        "cookies": [
            {
                "name": "session_id",
                "value": "test123",
                "domain": "example.com",
                "path": "/",
            }
        ],
        "origins": [],
    }

    test_netloc = "example.com"

    # Test saving
    try:
        crawler._save_cookies(test_netloc, test_storage_state)
        print(f"  ✓ Saved test cookies for {test_netloc}")
    except Exception as e:
        print(f"  ✗ Failed to save cookies: {e}")

    # Test loading
    try:
        loaded_state = crawler._load_cookies(test_netloc)
        if loaded_state:
            print(f"  ✓ Loaded cookies for {test_netloc}")
            print(f"  📊 Cookie count: {len(loaded_state.get('cookies', []))}")
        else:
            print(f"  ⚠️  No cookies found for {test_netloc}")
    except Exception as e:
        print(f"  ✗ Failed to load cookies: {e}")


def test_configuration():
    """Test and display the current cookie configuration."""

    print("=== Cookie Configuration ===")
    print(f"USE_PLAYWRIGHT_FALLBACK: {USE_PLAYWRIGHT_FALLBACK}")
    print(f"PLAYWRIGHT_COOKIE_PERSISTENCE: {PLAYWRIGHT_COOKIE_PERSISTENCE}")
    print(f"PLAYWRIGHT_MAX_CONCURRENCY: {PLAYWRIGHT_MAX_CONCURRENCY}")
    print(f"PLAYWRIGHT_NAV_TIMEOUT_MS: {PLAYWRIGHT_NAV_TIMEOUT_MS}")
    print()


if __name__ == "__main__":
    test_configuration()
    test_cookie_functions()
    test_cookie_handling()
