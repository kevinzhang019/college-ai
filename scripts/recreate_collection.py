#!/usr/bin/env python3
"""
Utility to drop the existing Milvus/Zilliz collection and recreate it
with the current crawler schema (including JSON majors field), and load it.
"""

import os
import sys

# Ensure project root on path and env loaded
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pymilvus import connections, utility
from college_ai.scraping.config import (
    ZILLIZ_URI,
    ZILLIZ_API_KEY,
    ZILLIZ_COLLECTION_NAME,
)
from college_ai.scraping.crawler import (
    MultithreadedCollegeCrawler,
)


def main() -> None:
    print("Connecting to Zilliz Cloud...")
    connections.connect(alias="default", uri=ZILLIZ_URI, token=ZILLIZ_API_KEY)
    print("✓ Connected")

    # Drop if exists
    if utility.has_collection(ZILLIZ_COLLECTION_NAME):
        print(f"Dropping collection '{ZILLIZ_COLLECTION_NAME}'...")
        utility.drop_collection(ZILLIZ_COLLECTION_NAME)
        print("✓ Dropped")
    else:
        print(f"Collection '{ZILLIZ_COLLECTION_NAME}' not found; will create new one.")

    # Recreate using crawler's schema setup
    print("Creating collection via crawler schema...")
    crawler = MultithreadedCollegeCrawler()
    fields = [f.name for f in crawler.collection.schema.fields]
    print(f"✓ Recreated collection '{ZILLIZ_COLLECTION_NAME}' with fields: {fields}")

    # Ensure loaded
    try:
        crawler.collection.load(timeout=120)
        print("✓ Collection loaded")
    except Exception as e:
        print(f"⚠️  Could not load collection immediately: {e}")


if __name__ == "__main__":
    main()
