#!/usr/bin/env python3
"""
Test script to demonstrate the improved Playwright fallback functionality.
This script tests various scenarios where Playwright fallback should be triggered.
"""

import os
import sys
import time
from urllib.parse import urlparse

# Add the crawlers directory to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "crawlers")))

from multithreaded_crawler import MultithreadedCollegeCrawler
from config import *


def test_playwright_fallback():
    """Test the Playwright fallback functionality with various scenarios."""

    print("=== Testing Playwright Fallback Functionality ===\n")

    # Initialize the crawler
    crawler = MultithreadedCollegeCrawler()

    # Test URLs that are likely to trigger Playwright fallback
    test_urls = [
        # JS-heavy SPA (likely to need Playwright)
        "https://reactjs.org/",
        # Modern web app with dynamic content
        "https://vuejs.org/",
        # Site that might have anti-bot measures
        "https://www.nytimes.com/",
        # Site with cookie banners and dynamic content
        "https://www.bbc.com/",
    ]

    print("Testing Playwright fallback with various URLs:")
    print(f"Playwright enabled: {crawler.playwright_enabled}")
    print(f"Playwright aggressive fallback: {crawler.playwright_aggressive_fallback}")
    print(f"Playwright max workers: {crawler.playwright_max_workers}")
    print()

    for i, url in enumerate(test_urls, 1):
        print(f"Test {i}: {url}")
        print("-" * 50)

        try:
            # Test regular scraping first
            print("1. Testing regular scraping...")
            start_time = time.time()
            page_data = crawler.scrape_page(url)
            regular_time = time.time() - start_time

            if page_data:
                print(f"   ✓ Regular scraping successful")
                print(f"   - Title: {page_data.get('title', 'N/A')[:50]}...")
                print(f"   - Content length: {len(page_data.get('content', ''))}")
                print(f"   - Word count: {page_data.get('word_count', 0)}")
                print(
                    f"   - Internal links: {len(page_data.get('internal_links', []))}"
                )
                print(f"   - Needs Playwright: {page_data.get('needs_pw', False)}")
                print(f"   - Time: {regular_time:.2f}s")

                # If Playwright fallback is needed, test it
                if page_data.get("needs_pw") and crawler.playwright_enabled:
                    print("\n2. Testing Playwright fallback...")
                    start_time = time.time()
                    pw_result = crawler._scrape_with_playwright(url)
                    pw_time = time.time() - start_time

                    if pw_result:
                        print(f"   ✓ Playwright fallback successful")
                        print(f"   - Title: {pw_result.get('title', 'N/A')[:50]}...")
                        print(
                            f"   - Content length: {len(pw_result.get('content', ''))}"
                        )
                        print(f"   - Word count: {pw_result.get('word_count', 0)}")
                        print(
                            f"   - Internal links: {len(pw_result.get('internal_links', []))}"
                        )
                        print(f"   - Time: {pw_time:.2f}s")

                        # Compare results
                        regular_content_len = len(page_data.get("content", ""))
                        pw_content_len = len(pw_result.get("content", ""))
                        improvement = pw_content_len - regular_content_len

                        if improvement > 0:
                            print(
                                f"   🎉 Playwright improved content by {improvement} characters"
                            )
                        elif improvement < 0:
                            print(
                                f"   ⚠️  Playwright reduced content by {abs(improvement)} characters"
                            )
                        else:
                            print(f"   ➖ No content change")
                    else:
                        print(f"   ✗ Playwright fallback failed")
                else:
                    print(f"   - Playwright fallback not needed or disabled")
            else:
                print(f"   ✗ Regular scraping failed")

                # Test Playwright fallback for failed scraping
                if crawler.playwright_enabled:
                    print("\n2. Testing Playwright fallback for failed scraping...")
                    start_time = time.time()
                    pw_result = crawler._scrape_with_playwright(url)
                    pw_time = time.time() - start_time

                    if pw_result:
                        print(f"   ✓ Playwright fallback successful")
                        print(f"   - Title: {pw_result.get('title', 'N/A')[:50]}...")
                        print(
                            f"   - Content length: {len(pw_result.get('content', ''))}"
                        )
                        print(f"   - Word count: {pw_result.get('word_count', 0)}")
                        print(
                            f"   - Internal links: {len(pw_result.get('internal_links', []))}"
                        )
                        print(f"   - Time: {pw_time:.2f}s")
                    else:
                        print(f"   ✗ Playwright fallback also failed")

        except Exception as e:
            print(f"   ✗ Error testing {url}: {e}")

        print("\n" + "=" * 60 + "\n")
        time.sleep(2)  # Be respectful to servers

    print("=== Playwright Fallback Test Complete ===")


def test_configuration():
    """Test and display the current Playwright configuration."""

    print("=== Playwright Configuration ===")
    print(f"USE_PLAYWRIGHT_FALLBACK: {USE_PLAYWRIGHT_FALLBACK}")
    print(f"PLAYWRIGHT_MAX_CONCURRENCY: {PLAYWRIGHT_MAX_CONCURRENCY}")
    print(f"PLAYWRIGHT_NAV_TIMEOUT_MS: {PLAYWRIGHT_NAV_TIMEOUT_MS}")
    print(f"PLAYWRIGHT_AGGRESSIVE_FALLBACK: {PLAYWRIGHT_AGGRESSIVE_FALLBACK}")
    print()

    # Check if Playwright is available
    try:
        from playwright.sync_api import sync_playwright

        print("✅ Playwright is available")
    except ImportError:
        print("❌ Playwright is not available (install with: pip install playwright)")
        print("   Then run: playwright install chromium")

    print()


if __name__ == "__main__":
    test_configuration()
    test_playwright_fallback()
