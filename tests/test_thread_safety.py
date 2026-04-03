#!/usr/bin/env python3
"""
Thread safety test for Playwright enhancements
"""

import sys
import os
import threading
import time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.scraping.crawler import (
    MultithreadedCollegeCrawler,
)


def test_thread_safety():
    """Test thread safety of Playwright features"""
    print("🧪 Testing thread safety of Playwright features...")

    try:
        # Create crawler instance
        crawler = MultithreadedCollegeCrawler(delay=1.0, max_workers=1)

        # Test URLs that should work with Playwright
        test_urls = [
            "https://example.com",
            "https://httpbin.org/html",
            "https://example.org",
        ]

        def test_single_thread(thread_id):
            """Test Playwright functionality in a single thread"""
            results = []
            print(f"Thread {thread_id}: Starting Playwright tests")

            for i, url in enumerate(test_urls):
                try:
                    print(f"Thread {thread_id}: Testing {url}")
                    result = crawler._scrape_with_playwright(url)

                    if result:
                        print(
                            f"Thread {thread_id}: ✅ Success for {url} - {result.get('word_count', 0)} words"
                        )
                        results.append(True)
                    else:
                        print(f"Thread {thread_id}: ❌ Failed for {url}")
                        results.append(False)

                    # Small delay to increase chance of thread conflicts
                    time.sleep(0.1)

                except Exception as e:
                    print(f"Thread {thread_id}: ❌ Error for {url}: {e}")
                    results.append(False)

            success_rate = sum(results) / len(results) if results else 0
            print(f"Thread {thread_id}: Completed with {success_rate:.1%} success rate")
            return results

        # Run multiple threads concurrently
        num_threads = 3
        print(f"\n🔄 Running {num_threads} concurrent threads...")

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            # Submit all thread tasks
            futures = [
                executor.submit(test_single_thread, thread_id)
                for thread_id in range(num_threads)
            ]

            # Wait for all to complete
            all_results = []
            for future in concurrent.futures.as_completed(futures):
                try:
                    thread_results = future.result(timeout=60)  # 60 second timeout
                    all_results.extend(thread_results)
                except Exception as e:
                    print(f"❌ Thread failed with error: {e}")
                    all_results.append(False)

        # Calculate overall success rate
        total_success = sum(all_results)
        total_tests = len(all_results)
        overall_success_rate = total_success / total_tests if total_tests > 0 else 0

        print(f"\n📊 Thread Safety Test Results:")
        print(f"   Total tests: {total_tests}")
        print(f"   Successful: {total_success}")
        print(f"   Success rate: {overall_success_rate:.1%}")

        # Test thread-local isolation
        print(f"\n🔍 Testing thread-local isolation...")

        def check_thread_isolation():
            """Verify that each thread gets its own Playwright instance"""
            try:
                # Force Playwright initialization
                result = crawler._scrape_with_playwright("https://example.com")

                # Check if thread-local variables exist
                has_pw = (
                    hasattr(crawler._pw_local, "pw")
                    and crawler._pw_local.pw is not None
                )
                has_browsers = hasattr(crawler._pw_local, "browsers")

                thread_id = threading.current_thread().ident
                print(
                    f"Thread {thread_id}: Playwright instance = {has_pw}, Browsers cache = {has_browsers}"
                )

                return has_pw and has_browsers
            except Exception as e:
                print(f"Thread isolation test error: {e}")
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            isolation_futures = [
                executor.submit(check_thread_isolation) for _ in range(2)
            ]
            isolation_results = [f.result() for f in isolation_futures]

        isolation_success = all(isolation_results)
        print(f"Thread isolation: {'✅ PASS' if isolation_success else '❌ FAIL'}")

        # Overall assessment
        success = overall_success_rate > 0.7 and isolation_success
        print(f"\n🎯 Overall Assessment: {'✅ PASS' if success else '❌ FAIL'}")

        return success

    except Exception as e:
        print(f"❌ Thread safety test error: {e}")
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
    success = test_thread_safety()
    sys.exit(0 if success else 1)
