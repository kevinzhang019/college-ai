#!/usr/bin/env python3
"""
Utility to drop the existing Milvus/Zilliz collection and recreate it
with the current hybrid search schema (dense + BM25, page_type, COSINE).

Uses the crawler's get_or_create_collection() which handles the full
schema creation including BM25 function, INVERTED indexes, etc.
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pymilvus import connections, utility
from college_ai.scraping.config import (
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME,
)


def main() -> None:
    print("Connecting to Zilliz Cloud...")
    connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
    print("  Connected")

    # Drop if exists
    if utility.has_collection(ZILLIZ_COLLECTION_NAME):
        print(f"Dropping collection '{ZILLIZ_COLLECTION_NAME}'...")
        utility.drop_collection(ZILLIZ_COLLECTION_NAME)
        print("  Dropped")
    else:
        print(f"Collection '{ZILLIZ_COLLECTION_NAME}' not found; will create new one.")

    # Recreate using crawler's hybrid schema setup
    print("Creating collection with hybrid search schema...")
    from college_ai.scraping.crawler import MultithreadedCollegeCrawler
    crawler = MultithreadedCollegeCrawler()
    fields = [f.name for f in crawler.collection.schema.fields]
    print(f"  Recreated '{ZILLIZ_COLLECTION_NAME}' with fields: {fields}")

    # Ensure loaded
    try:
        crawler.collection.load(timeout=120)
        print("  Collection loaded")
    except Exception as e:
        print(f"  Could not load collection immediately: {e}")


if __name__ == "__main__":
    main()
