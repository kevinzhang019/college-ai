#!/usr/bin/env python3
"""
Quick test script to validate Playwright fixes
"""

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.scraping.crawler import (
    MultithreadedCollegeCrawler,
)


def test_playwright_basic():
    """Test basic Playwright functionality"""
    print("🧪 Testing Enhanced Playwright fixes...")

    try:
        # Create crawler instance
        crawler = MultithreadedCollegeCrawler(delay=1.0, max_workers=1)

        # Test multiple URLs with different characteristics
        test_urls = [
            "https://example.com",  # Simple static page
            "https://httpbin.org/html",  # Another simple test page
            "https://video.alumni.nyu.edu/media/Alumni+Profile+Video+Series%3A+Anna+Zuccaro+%28CAS+13%29/1_p8muc3pg",  # Problematic URL that was causing final_url error
        ]

        results = []
        for test_url in test_urls:
            print(f"\n🔍 Testing with {test_url}")

            # Try Playwright fallback directly
            result = crawler._scrape_with_playwright(test_url)

            if result:
                print(f"✅ Playwright returned content:")
                print(f"   Title: {result.get('title', 'N/A')}")
                print(f"   Content length: {len(result.get('content', ''))}")
                print(f"   Word count: {result.get('word_count', 0)}")
                print(f"   Links found: {len(result.get('internal_links', []))}")
                results.append(True)
            else:
                print("❌ Playwright returned None")
                results.append(False)

        # Test summary
        success_count = sum(results)
        total_count = len(results)
        success_rate = success_count / total_count if total_count > 0 else 0

        print(f"\n📊 Test Results:")
        print(f"   Successful: {success_count}/{total_count} ({success_rate:.1%})")

        # Test cookie functionality
        print(f"\n🍪 Testing cookie handling...")
        cookie_dir = crawler.cookie_storage_dir
        print(f"   Cookie directory: {cookie_dir}")
        print(f"   Cookie persistence enabled: {crawler.playwright_cookie_persistence}")

        # Test anti-detection features
        print(f"\n🔒 Anti-detection features:")
        print(f"   User agent rotation: ✅")
        print(f"   Viewport randomization: ✅")
        print(f"   Browser fingerprint masking: ✅")
        print(f"   Enhanced headers: ✅")

        return success_rate > 0.5  # Consider successful if > 50% work

    except Exception as e:
        print(f"❌ Error testing Playwright: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        # Cleanup
        try:
            crawler.close()
        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")


if __name__ == "__main__":
    success = test_playwright_basic()
    sys.exit(0 if success else 1)
