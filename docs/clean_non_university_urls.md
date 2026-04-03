# Clean Non-University URLs Script

This script identifies and removes records from the Milvus collection where the URLs don't belong to the university domain of their associated college. It handles chunked pages (multiple records per URL) appropriately.

## Purpose

When crawling university websites, sometimes the crawler can follow links to external domains outside the university. This script helps clean up the database by:

1. Identifying the base domain for each university
2. Finding records with URLs that don't match that university's domain
3. Removing those records to maintain data quality

## Features

- Analyzes each college separately to ensure proper domain matching
- Handles subdomains appropriately (e.g., cs.stanford.edu is valid for stanford.edu)
- Processes records in batches for efficiency
- Supports dry-run mode to preview changes before deleting
- Generates a detailed report of findings
- Can process all colleges or a single specified college

## Usage

### Basic Usage (All Colleges)

This will scan all colleges and remove records with non-university URLs:

```bash
python scripts/clean_non_university_urls.py
```

### Dry Run Mode

To check what would be removed without actually deleting anything:

```bash
python scripts/clean_non_university_urls.py --dry-run
```

### Process a Single College

To clean records for a specific college only:

```bash
python scripts/clean_non_university_urls.py --college "Stanford University"
```

### Analysis Mode

To analyze the non-university domains by category without deleting:

```bash
python scripts/clean_non_university_urls.py --analyze-only
```

This will categorize domains into groups like social media, academic publishers, etc. and provide a detailed breakdown.

### Troubleshooting Modes

If records are not being deleted despite the script running successfully, try these special modes:

#### Force Recheck Mode

```bash
python scripts/clean_non_university_urls.py --force-recheck
```

This ensures deletion is properly verified and provides debugging information if records persist.

#### Permission Override Mode

If you suspect your connection has limited permissions with Zilliz/Milvus:

```bash
python scripts/clean_non_university_urls.py --force-permissions
```

This attempts alternative deletion methods that might work with restricted permissions, including:

- Direct entity deletion using ID-based methods
- Overwriting records via upsert instead of deletion
- More detailed diagnostics about the connection status

## Output

The script generates:

1. Console output showing progress and summary statistics
2. A CSV report file (`non_university_urls_report.csv`) with detailed findings per college

## Example Output

```
Processing college: Stanford University
  Base university domain: stanford.edu
  Found 23 non-university URLs out of 5432 total records
  Examples of non-university URLs:
    - https://github.com/stanfordnlp/CoreNLP
    - https://twitter.com/Stanford
    - https://www.youtube.com/user/StanfordUniversity
  Removed 45 records with non-university URLs

SUMMARY
================================================================================
Total records removed: 45

Detailed results by college:
    college_name university_domain total_records non_university_urls                                  example_bad_urls
Stanford University      stanford.edu          5432                 23 https://github.com/stanfordnlp/CoreNLP; https://twitter.com/Stanford; https://www.youtube.com/user/StanfordUniversity

Detailed report saved to non_university_urls_report.csv
```

## Notes

- The script uses domain validation logic to properly handle subdomains
- It treats .edu, .ac.uk, and similar educational TLDs as potential university domains
- It includes safeguards for batch processing to handle large datasets
- The script connects to Zilliz Cloud using the environment variables in your configuration

## Environment Requirements

Make sure the following environment variables are set in your `.env` file or environment:

```
ZILLIZ_URI=https://your-instance.zillizcloud.com
ZILLIZ_API_KEY=your_api_key
ZILLIZ_COLLECTION_NAME=college_pages
```

These variables are read from the same configuration used by the crawler.
