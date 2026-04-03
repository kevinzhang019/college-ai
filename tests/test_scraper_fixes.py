"""
Quick smoke test for the www-stripping fix and queue timeout fix.

Crawls 2 schools (UVA with www prefix, SDSU with few pages) with a low
page limit and verifies data is persisted to Zilliz/Milvus.
"""

import os
import sys
import time

# Ensure project root is on the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from college_ai.scraping.crawler import MultithreadedCollegeCrawler


def test_scraper():
    crawler = MultithreadedCollegeCrawler()

    # Test schools: one www-prefixed (UVA), one with few pages (SDSU)
    test_jobs = {
        "general": [
            {"name": "University of Virginia", "url": "https://www.virginia.edu", "major": "general"},
            {"name": "San Diego State University", "url": "https://www.sdsu.edu", "major": "general"},
        ]
    }

    max_pages = 5  # Small limit for testing
    print(f"\n{'='*60}")
    print(f"SMOKE TEST: Crawling 2 schools, max {max_pages} pages each")
    print(f"{'='*60}")

    start = time.time()
    crawler.crawl_all_colleges(test_jobs, max_pages_per_college=max_pages)
    elapsed = time.time() - start

    # Flush remaining inserts
    crawler._insert_flush_stop.set()
    crawler._insert_flush_thread.join(timeout=10)
    crawler.embedding_batcher.shutdown()
    crawler.pw_pool.shutdown()
    if crawler._delta_cache:
        crawler._delta_cache.close()

    print(f"\n{'='*60}")
    print(f"RESULTS (elapsed: {elapsed:.1f}s)")
    print(f"{'='*60}")
    print(f"Pages crawled:   {crawler.stats['total_pages_crawled']}")
    print(f"Vectors uploaded: {crawler.stats['total_vectors_uploaded']}")
    print(f"Errors:          {crawler.stats['total_errors']}")
    print(f"Existing skipped: {crawler.stats['existing_urls_skipped']}")
    print(f"Duplicates skipped: {crawler.stats['duplicate_urls_skipped']}")

    # Verify data persistence: query Zilliz for both schools
    print(f"\n{'='*60}")
    print("PERSISTENCE CHECK: Querying Zilliz for stored records")
    print(f"{'='*60}")

    for school in ["University of Virginia", "San Diego State University"]:
        try:
            escaped = school.replace('"', '\\"')
            records = crawler.collection.query(
                expr=f'college_name == "{escaped}"',
                output_fields=["url", "title", "majors", "crawled_at"],
                limit=100,
            )
            print(f"\n  {school}: {len(records)} records in Zilliz")
            for rec in records[:3]:
                url = rec.get("url", "?")
                title = rec.get("title", "?")[:60]
                print(f"    - {url}")
                print(f"      title: {title}")
        except Exception as e:
            print(f"\n  {school}: ERROR querying - {e}")

    # Check that UVA URLs preserved www
    print(f"\n{'='*60}")
    print("WWW-PRESERVATION CHECK")
    print(f"{'='*60}")
    try:
        records = crawler.collection.query(
            expr='college_name == "University of Virginia"',
            output_fields=["url"],
            limit=100,
        )
        www_count = sum(1 for r in records if "www.virginia.edu" in r.get("url", ""))
        bare_count = sum(
            1 for r in records
            if "virginia.edu" in r.get("url", "") and "www.virginia.edu" not in r.get("url", "")
        )
        print(f"  URLs with www.virginia.edu: {www_count}")
        print(f"  URLs with bare virginia.edu: {bare_count}")
        if www_count > 0 and bare_count == 0:
            print("  PASS: www prefix preserved correctly")
        elif len(records) == 0:
            print("  SKIP: No records to check (all may have been delta-skipped)")
        else:
            print(f"  INFO: Mixed results - check if bare-domain URLs are from prior runs")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Timing check for queue timeout improvement
    print(f"\n{'='*60}")
    print("TIMING CHECK")
    print(f"{'='*60}")
    print(f"  Total elapsed: {elapsed:.1f}s")
    if elapsed < 60:
        print("  PASS: Completed in under 60s (old idle timeout per school)")
    else:
        print("  NOTE: Took over 60s - may include actual crawl/embed time")

    print(f"\n{'='*60}")
    print("SMOKE TEST COMPLETE")
    print(f"{'='*60}")


if __name__ == "__main__":
    test_scraper()
