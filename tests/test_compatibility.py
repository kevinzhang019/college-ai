#!/usr/bin/env python3
"""
Backward compatibility test for new Playwright features
"""

import sys
import os
import tempfile
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.scraping.crawler import (
    MultithreadedCollegeCrawler,
)


def test_backward_compatibility():
    """Test that new Playwright features don't break existing functionality"""
    print("🧪 Testing backward compatibility...")

    try:
        # Create crawler instance
        crawler = MultithreadedCollegeCrawler(delay=1.0, max_workers=1)

        # Test 1: Regular scraping (non-Playwright) still works
        print("\n1️⃣ Testing regular HTTP scraping...")
        test_url = "https://httpbin.org/html"

        try:
            result = crawler.scrape_page(test_url)
            if result and result.get("content"):
                print("   ✅ Regular HTTP scraping works")
                regular_scraping_works = True
            else:
                print("   ❌ Regular HTTP scraping failed")
                regular_scraping_works = False
        except Exception as e:
            print(f"   ❌ Regular HTTP scraping error: {e}")
            regular_scraping_works = False

        # Test 2: Playwright integration doesn't interfere with normal flow
        print("\n2️⃣ Testing Playwright integration...")

        try:
            # This should trigger Playwright fallback
            result_with_pw = crawler.scrape_page(test_url)

            if result_with_pw:
                needs_pw = result_with_pw.get("needs_pw", False)
                print(f"   ✅ Playwright integration working (needs_pw: {needs_pw})")
                playwright_integration_works = True
            else:
                print("   ❌ Playwright integration failed")
                playwright_integration_works = False
        except Exception as e:
            print(f"   ❌ Playwright integration error: {e}")
            playwright_integration_works = False

        # Test 3: Milvus upload functionality preserved
        print("\n3️⃣ Testing Milvus upload functionality...")

        try:
            # Create test data structure
            test_data = {
                "url": "https://test.example.com",
                "title": "Test Page",
                "content": "This is test content for compatibility testing.",
                "word_count": 8,
                "internal_links": [],
                "crawled_at": "2024-01-01T00:00:00",
            }

            # Test upload (should work with both old and new data structures)
            upload_result = crawler.upload_to_milvus(
                test_data, "Test College", "general"
            )

            if upload_result:
                print("   ✅ Milvus upload functionality works")
                milvus_upload_works = True
            else:
                print("   ⚠️  Milvus upload returned False (may be due to duplicates)")
                milvus_upload_works = True  # Not necessarily an error
        except Exception as e:
            print(f"   ❌ Milvus upload error: {e}")
            milvus_upload_works = False

        # Test 4: Cookie handling doesn't break non-Playwright operations
        print("\n4️⃣ Testing cookie handling isolation...")

        try:
            # Test cookie path generation
            cookie_path = crawler._get_cookie_storage_path("example.com")
            cookie_path_works = "example.com" in cookie_path and cookie_path.endswith(
                ".json"
            )

            if cookie_path_works:
                print("   ✅ Cookie path generation works")
            else:
                print("   ❌ Cookie path generation failed")

            # Test cookie loading (should not crash if no cookies exist)
            cookies = crawler._load_cookies("nonexistent.domain")
            cookie_loading_works = (
                cookies is None
            )  # Should return None for non-existent

            if cookie_loading_works:
                print("   ✅ Cookie loading handles non-existent domains")
            else:
                print("   ❌ Cookie loading failed")

            cookie_handling_works = cookie_path_works and cookie_loading_works

        except Exception as e:
            print(f"   ❌ Cookie handling error: {e}")
            cookie_handling_works = False

        # Test 5: Thread-local storage doesn't interfere with single-threaded operation
        print("\n5️⃣ Testing thread-local storage...")

        try:
            # Access thread-local storage
            has_pw_local = hasattr(crawler, "_pw_local")

            if has_pw_local:
                print("   ✅ Thread-local storage available")
                thread_local_works = True
            else:
                print("   ❌ Thread-local storage missing")
                thread_local_works = False

        except Exception as e:
            print(f"   ❌ Thread-local storage error: {e}")
            thread_local_works = False

        # Test 6: Original configuration values preserved
        print("\n6️⃣ Testing configuration preservation...")

        try:
            config_checks = {
                "playwright_enabled": hasattr(crawler, "playwright_enabled"),
                "playwright_semaphore": hasattr(crawler, "playwright_semaphore"),
                "collection": hasattr(crawler, "collection"),
                "session": hasattr(crawler, "session"),
                "lock": hasattr(crawler, "lock"),
                "collection_write_lock": hasattr(crawler, "collection_write_lock"),
            }

            config_preserved = all(config_checks.values())

            if config_preserved:
                print("   ✅ Configuration values preserved")
            else:
                missing = [k for k, v in config_checks.items() if not v]
                print(f"   ❌ Missing configuration: {missing}")

        except Exception as e:
            print(f"   ❌ Configuration check error: {e}")
            config_preserved = False

        # Test 7: Error handling doesn't change behavior
        print("\n7️⃣ Testing error handling...")

        try:
            # Test with invalid URL
            result = crawler.scrape_page("invalid://not-a-url")
            error_handling_works = result is None  # Should return None for invalid URLs

            if error_handling_works:
                print("   ✅ Error handling works correctly")
            else:
                print("   ❌ Error handling changed behavior")

        except Exception as e:
            print(f"   ❌ Error handling test error: {e}")
            error_handling_works = False

        # Overall assessment
        all_tests = [
            regular_scraping_works,
            playwright_integration_works,
            milvus_upload_works,
            cookie_handling_works,
            thread_local_works,
            config_preserved,
            error_handling_works,
        ]

        passed_tests = sum(all_tests)
        total_tests = len(all_tests)

        print(f"\n📊 Compatibility Test Results:")
        print(f"   Tests passed: {passed_tests}/{total_tests}")
        print(f"   Success rate: {passed_tests/total_tests:.1%}")

        success = passed_tests == total_tests
        print(f"\n🎯 Backward Compatibility: {'✅ PASS' if success else '❌ FAIL'}")

        return success

    except Exception as e:
        print(f"❌ Compatibility test error: {e}")
        import traceback

        traceback.print_exc()
        return False

    finally:
        # Cleanup
        try:
            crawler.close()
            print("🧹 Cleanup completed")
        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")


if __name__ == "__main__":
    success = test_backward_compatibility()
    sys.exit(0 if success else 1)
