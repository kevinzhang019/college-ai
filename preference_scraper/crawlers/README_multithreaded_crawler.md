# Multithreaded College Crawler

Crawl college websites in parallel and store each page (with embeddings) in Milvus.

## Quick Start

```bash
# 1. install deps
pip install -r requirements.txt

# 2. run crawler
python preference_scraper/crawlers/multithreaded_crawler.py
```

CSV files live in `preference_scraper/crawlers/colleges/` and look like:

```csv
name,url
"Harvard Business School","https://www.hbs.edu/"
```

## Customize

```python
from preference_scraper.crawlers.multithreaded_crawler import MultithreadedCollegeCrawler

crawler = MultithreadedCollegeCrawler(delay=1.0, max_workers=4)
crawler.run_full_crawling_pipeline(max_pages_per_college=30)
```

## What Happens

1. Read college list from CSV.
2. Crawl pages concurrently (ThreadPoolExecutor).
3. Extract text, make OpenAI embeddings.
4. Save into `college_pages` collection in Milvus.

That’s it — happy crawling!