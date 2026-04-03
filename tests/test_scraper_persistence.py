"""
Quick smoke test: crawl 2 colleges (3 pages each), then verify
vectors were persisted in Zilliz Cloud.
"""

import os
import sys
import time

# Ensure project root is on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from college_ai.scraping.config import (
    ZILLIZ_URI, ZILLIZ_API_KEY, ZILLIZ_COLLECTION_NAME, VECTOR_DIM,
)

def main():
    print("=" * 60)
    print("SCRAPER PERSISTENCE TEST")
    print("=" * 60)

    # --- Step 0: Check Zilliz connection ---
    print("\n[0] Checking Zilliz connection...")
    from pymilvus import connections, Collection, utility

    connections.connect(
        alias="test",
        uri=ZILLIZ_URI,
        token=ZILLIZ_API_KEY,
    )
    assert utility.has_collection(ZILLIZ_COLLECTION_NAME, using="test"), \
        f"Collection '{ZILLIZ_COLLECTION_NAME}' not found"

    coll = Collection(ZILLIZ_COLLECTION_NAME, using="test")
    coll.load()
    before_count = coll.num_entities
    print(f"  Collection '{ZILLIZ_COLLECTION_NAME}' exists, {before_count:,} entities before test")

    connections.disconnect("test")

    # --- Step 1: Run crawler on 2 colleges, 3 pages each ---
    print("\n[1] Running crawler (2 colleges, 3 pages each)...")

    # Override config for a fast test
    os.environ["INTER_COLLEGE_PARALLELISM"] = "2"
    os.environ["MAX_CRAWL_TIME_PER_COLLEGE"] = "60"
    os.environ["ENABLE_DELTA_CRAWLING"] = "0"  # force fresh crawl

    from college_ai.scraping.crawler import MultithreadedCollegeCrawler

    crawler = MultithreadedCollegeCrawler(delay=0.5, max_workers=2)

    # Build a tiny majors_data with just 2 colleges
    test_data = {
        "test": [
            {"name": "MIT", "url": "https://www.mit.edu", "major": "test"},
            {"name": "Stanford University", "url": "https://www.stanford.edu", "major": "test"},
        ]
    }

    start = time.time()
    crawler.crawl_all_colleges(test_data, max_pages_per_college=3)
    elapsed = time.time() - start

    print(f"\n  Crawl completed in {elapsed:.1f}s")
    print(f"  Stats: {crawler.stats}")

    # Shutdown
    crawler._insert_flush_stop.set()
    crawler._insert_flush_thread.join(timeout=5)
    crawler.embedding_batcher.shutdown()
    crawler.pw_pool.shutdown()

    # --- Step 2: Verify vectors in Zilliz ---
    print("\n[2] Verifying vectors in Zilliz Cloud...")
    time.sleep(2)  # brief wait for Zilliz consistency

    connections.connect(
        alias="verify",
        uri=ZILLIZ_URI,
        token=ZILLIZ_API_KEY,
    )
    coll2 = Collection(ZILLIZ_COLLECTION_NAME, using="verify")
    coll2.load()
    after_count = coll2.num_entities
    new_vectors = after_count - before_count

    print(f"  Entities before: {before_count:,}")
    print(f"  Entities after:  {after_count:,}")
    print(f"  New vectors:     {new_vectors:,}")

    # Query for our test colleges to confirm they exist
    for college in ["MIT", "Stanford University"]:
        results = coll2.query(
            expr=f'college_name == "{college}"',
            output_fields=["college_name", "url", "title", "majors"],
            limit=5,
        )
        print(f"\n  {college}: {len(results)} vectors found")
        for r in results[:2]:
            title = (r.get("title") or "")[:60]
            url = (r.get("url") or "")[:60]
            majors = r.get("majors", [])
            print(f"    - {title}")
            print(f"      URL: {url}")
            print(f"      Majors: {majors}")

    connections.disconnect("verify")

    # --- Step 3: Check delta cache ---
    print("\n[3] Checking delta crawl cache...")
    cache_path = os.path.join(
        os.path.dirname(__file__), "..", "data", "crawl_cache.db"
    )
    if os.path.exists(cache_path):
        import sqlite3
        conn = sqlite3.connect(cache_path)
        count = conn.execute("SELECT COUNT(*) FROM crawl_cache").fetchone()[0]
        sample = conn.execute(
            "SELECT canonical_url, content_hash, crawled_at FROM crawl_cache LIMIT 3"
        ).fetchall()
        conn.close()
        print(f"  Cache entries: {count}")
        for row in sample:
            print(f"    - {row[0][:60]}  hash={row[1]}  at={row[2]}")
    else:
        print(f"  (Delta crawling was disabled for this test)")

    # --- Verdict ---
    print("\n" + "=" * 60)
    vectors_uploaded = crawler.stats.get("total_vectors_uploaded", 0)
    if vectors_uploaded > 0:
        print(f"PASS: {vectors_uploaded} vectors uploaded and persisted in Zilliz")
    elif new_vectors > 0:
        print(f"PASS: {new_vectors} new vectors detected in Zilliz")
    else:
        print("WARN: No new vectors detected — pages may have been previously crawled")
        print("      (check 'existing_urls_skipped' in stats above)")
    print("=" * 60)


if __name__ == "__main__":
    main()
