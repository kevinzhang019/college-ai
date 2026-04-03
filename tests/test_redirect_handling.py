#!/usr/bin/env python3
"""
Test script for redirect handling in the Playwright crawler.
Tests various redirect scenarios to ensure the fixes work correctly.
"""

import os
import sys
import json
from typing import List, Dict

# Add the parent directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.scraping.crawler import MultithreadedCollegeCrawler


def test_redirect_urls() -> List[str]:
    """Return a list of URLs known to redirect for testing."""
    return [
        "http://httpbin.org/redirect/3",  # Known redirect test endpoint
        "http://httpbin.org/redirect-to?url=http://httpbin.org/get",  # Redirect to specific URL
        "https://bit.ly/3X1Y2Z3",  # URL shortener (likely redirects)
        "https://www.google.com/url?q=https://example.com",  # Google redirect
        # Add college URLs that you know redirect
        "https://admissions.northeastern.edu",  # Often redirects to specific pages
        "https://apply.arizona.edu",  # Often redirects
    ]


def test_single_url(crawler: MultithreadedCollegeCrawler, url: str) -> Dict:
    """Test a single URL for redirect handling."""
    print(f"\n🧪 Testing redirect handling for: {url}")
    print("=" * 60)

    try:
        # Test Playwright fallback specifically
        result = crawler._scrape_with_playwright(url)

        if result:
            print(f"✅ Success!")
            print(f"   Original URL: {url}")
            print(f"   Final URL: {result['url']}")
            print(
                f"   Original URL (if redirected): {result.get('original_url', 'None')}"
            )
            print(f"   Redirect detected: {result.get('redirect_detected', False)}")
            print(f"   Title: {result['title'][:100]}...")
            print(f"   Content length: {len(result['content'])} chars")
            print(f"   Word count: {result['word_count']}")
            print(f"   Links found: {len(result['internal_links'])}")

            return {
                "url": url,
                "status": "success",
                "final_url": result["url"],
                "redirected": result.get("redirect_detected", False),
                "content_length": len(result["content"]),
                "word_count": result["word_count"],
            }
        else:
            print(f"❌ Failed - No result returned")
            return {"url": url, "status": "failed", "error": "No result returned"}

    except Exception as e:
        print(f"❌ Failed with exception: {e}")
        return {"url": url, "status": "error", "error": str(e)}


def main():
    """Run redirect handling tests."""
    print("🚀 Starting Playwright Redirect Handling Tests")
    print("=" * 60)

    # Initialize crawler
    crawler = MultithreadedCollegeCrawler()

    # Check if Playwright is available
    try:
        from playwright.sync_api import sync_playwright

        print("✅ Playwright is available")
    except ImportError:
        print("❌ Playwright is not available - cannot run tests")
        return

    # Test URLs
    test_urls = test_redirect_urls()
    results = []

    for url in test_urls:
        result = test_single_url(crawler, url)
        results.append(result)

    # Summary
    print("\n📊 Test Summary")
    print("=" * 60)

    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] in ["failed", "error"])
    redirect_count = sum(1 for r in results if r.get("redirected", False))

    print(f"Total tests: {len(results)}")
    print(f"Successful: {success_count}")
    print(f"Failed: {failed_count}")
    print(f"Redirects detected: {redirect_count}")

    # Save results
    results_file = "redirect_test_results.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n💾 Results saved to {results_file}")

    # Show redirect examples
    redirected_results = [r for r in results if r.get("redirected", False)]
    if redirected_results:
        print(f"\n🔄 Redirected URLs ({len(redirected_results)}):")
        for r in redirected_results:
            print(f"   {r['url']} -> {r.get('final_url', 'Unknown')}")


if __name__ == "__main__":
    main()
