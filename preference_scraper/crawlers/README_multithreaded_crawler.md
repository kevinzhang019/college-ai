# Multithreaded College Crawler

A high-performance multithreaded crawler that reads college URLs from CSV files, crawls each site using BeautifulSoup to find internal links, and uploads each page directly to Milvus with embeddings.

## Features

- **Multithreaded Crawling**: Uses ThreadPoolExecutor for concurrent page crawling
- **Internal Link Discovery**: Uses BeautifulSoup to find all internal links from each page
- **Direct Milvus Upload**: Uploads each page to Milvus immediately after crawling (no JSON storage)
- **Smart URL Filtering**: Filters out irrelevant URLs (images, admin pages, external links)
- **Respectful Crawling**: Configurable delays between requests
- **Comprehensive Statistics**: Tracks crawling progress and success rates

## Requirements

- Python 3.7+
- Milvus database running on localhost:19530
- Required Python packages (see requirements.txt):
  - requests
  - beautifulsoup4
  - pymilvus
  - openai (for embeddings)

## Setup

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Start Milvus**:
   ```bash
   docker-compose up -d
   ```

3. **Prepare CSV files**:
   Create CSV files in the `preference_scraper/crawlers/colleges/` directory with the following format:
   ```csv
   name,url
   "Harvard Business School","https://www.hbs.edu/"
   "Stanford GSB","https://www.gsb.stanford.edu/"
   ```

## Usage

### Quick Start

Run the crawler with default settings:
```bash
python preference_scraper/crawlers/multithreaded_crawler.py
```

### Interactive Runner

Use the interactive runner for easy configuration:
```bash
python preference_scraper/crawlers/run_crawler.py
```

### Test Mode

Test with a single college:
```bash
python preference_scraper/crawlers/test_multithreaded_crawler.py
```

## Configuration

You can modify the crawler settings in the code:

```python
crawler = MultithreadedCollegeCrawler(
    delay=1.0,           # Delay between requests (seconds)
    max_workers=4        # Number of worker threads per college
)

crawler.run_full_crawling_pipeline(
    max_pages_per_college=30  # Maximum pages to crawl per college
)
```

## How It Works

1. **CSV Reading**: Reads college URLs from CSV files organized by major
2. **Site Crawling**: For each college:
   - Starts with the base URL
   - Uses BeautifulSoup to extract all internal links
   - Crawls pages in batches using multiple threads
   - Discovers new links from each crawled page
3. **Content Processing**: For each page:
   - Extracts title and main content
   - Cleans and processes text
   - Generates embeddings using OpenAI
4. **Milvus Upload**: Immediately uploads each page to Milvus with:
   - Unique ID
   - College name and major
   - URL and title
   - Cleaned content
   - Embedding vector
   - Timestamp

## Milvus Schema

The crawler creates a `college_pages` collection with the following schema:

- `id` (VARCHAR): Unique identifier
- `college_name` (VARCHAR): Name of the college
- `url` (VARCHAR): Page URL
- `title` (VARCHAR): Page title
- `content` (VARCHAR): Cleaned page content
- `embedding` (FLOAT_VECTOR): 1536-dimensional embedding
- `crawled_at` (VARCHAR): Timestamp
- `major` (VARCHAR): Academic major category

## Performance

- **Concurrent Processing**: Multiple threads crawl different pages simultaneously
- **Immediate Upload**: No intermediate storage, direct upload to Milvus
- **Smart Filtering**: Only processes relevant internal links
- **Memory Efficient**: Processes pages one at a time, doesn't store all data in memory

## Monitoring

The crawler provides real-time progress updates:

```
=== Crawling Harvard Business School (business) ===
Base URL: https://www.hbs.edu/
  Crawling batch of 4 URLs...
    Crawling: https://www.hbs.edu/
    ✓ Uploaded to Milvus: https://www.hbs.edu/
    Crawling: https://www.hbs.edu/programs
    ✓ Uploaded to Milvus: https://www.hbs.edu/programs
```

## Error Handling

- **Network Errors**: Gracefully handles connection timeouts and HTTP errors
- **Parsing Errors**: Continues crawling even if individual pages fail to parse
- **Milvus Errors**: Logs upload failures but continues processing
- **Rate Limiting**: Respectful delays prevent overwhelming target servers

## Output

All crawled data is stored directly in Milvus and can be queried using vector similarity search:

```python
# Example query (implement in your application)
results = collection.search(
    data=[query_embedding],
    anns_field="embedding",
    param={"metric_type": "L2", "params": {"nprobe": 10}},
    limit=10
)
```

## Troubleshooting

1. **Milvus Connection Error**: Ensure Milvus is running on localhost:19530
2. **CSV File Not Found**: Check that CSV files exist in the colleges/ directory
3. **Rate Limiting**: Increase delay between requests if getting blocked
4. **Memory Issues**: Reduce max_pages_per_college or max_workers

## Example CSV Files

### business.csv
```csv
name,url
"Harvard Business School","https://www.hbs.edu/"
"Stanford Graduate School of Business","https://www.gsb.stanford.edu/"
"Wharton School","https://www.wharton.upenn.edu/"
```

### computer_science.csv
```csv
name,url
"MIT EECS","https://www.eecs.mit.edu/"
"Stanford CS","https://cs.stanford.edu/"
"Carnegie Mellon SCS","https://www.cs.cmu.edu/"
``` 