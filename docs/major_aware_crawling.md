# Major-Aware Crawling Logic

This document describes the enhanced crawling logic that intelligently handles URLs that exist in the database but don't have the current major.

## Overview

The crawler now uses **major-aware logic** to determine whether to crawl a URL or skip it. Instead of simply checking if a URL exists in the canonical URLs set, it checks if the URL exists **with the current major**.

## Problem Solved

### Previous Behavior (Problematic)

- URLs were skipped if they existed in the canonical URLs set
- This prevented adding new majors to existing pages
- Links from existing pages weren't discovered for new majors
- Poor coverage across different majors

### New Behavior (Improved)

- URLs are only skipped if they exist **with the current major**
- URLs that exist but don't have the current major are crawled
- The `upload_to_milvus` function handles adding the new major to existing records
- Links are discovered and added to the queue for BFS traversal

## Implementation Details

### New Helper Function

```python
def _check_url_has_major(self, url_canonical: str, major: str) -> bool:
    """Check if a URL already exists with the specified major.

    Args:
        url_canonical: Canonical URL key to check
        major: Major to check for

    Returns:
        True if URL exists with the major, False otherwise
    """
```

### Updated Crawling Logic

```python
# Check if URL exists with current major - only skip if it does
if canon_key in self.college_canonical_urls:
    # URL exists, check if it has the current major
    if self._check_url_has_major(canon_key, major):
        # URL exists with current major - skip crawling
        with self.lock:
            self.stats["existing_urls_skipped"] += 1
        continue
    else:
        # URL exists but doesn't have current major - continue crawling
        # The upload_to_milvus function will handle adding the major
        print(f"    🔄 URL exists but missing major '{major}', continuing crawl: {url}")
```

### Updated Link Filtering

```python
# Check if link exists with current major
link_has_major = False
if canon_link in self.college_canonical_urls:
    link_has_major = self._check_url_has_major(canon_link, major)

already_seen = (
    link_has_major
    or canon_link in crawled_canon
    or canon_link in discovered_canon
)
```

## Behavior Examples

### Example 1: URL exists with current major

```
URL: https://example.edu/computer-science
Current major: computer_science
Database: URL exists with computer_science major
Result: SKIP crawling (already have this content for this major)
```

### Example 2: URL exists but missing current major

```
URL: https://example.edu/computer-science
Current major: business
Database: URL exists with computer_science major only
Result: CONTINUE crawling (need to add business major)
```

### Example 3: URL doesn't exist

```
URL: https://example.edu/new-page
Current major: computer_science
Database: URL doesn't exist
Result: CONTINUE crawling (new content)
```

## Benefits

### 1. **Better Coverage Across Majors**

- Pages are crawled for each major that needs them
- No content is missed due to premature skipping

### 2. **Efficient Major Addition**

- Existing records are updated with new majors
- No duplicate content is created
- The `upload_to_milvus` function handles the upsert logic

### 3. **Maintained BFS Discovery**

- Links from pages are discovered for new majors
- Breadth-first search continues properly
- New pages are found even from existing pages

### 4. **Performance Optimization**

- Only necessary URLs are crawled
- URLs that already have the current major are skipped
- Database queries are minimized

## Process Flow

### 1. URL Check

```
URL encountered → Check if in canonical URLs set
                ↓
            If exists → Check if has current major
                ↓
            If has major → Skip crawling
            If missing major → Continue crawling
```

### 2. Link Discovery

```
Page crawled → Extract internal links
            ↓
        For each link → Check if exists with current major
                    ↓
                If has major → Skip adding to queue
                If missing major → Add to queue for BFS
```

### 3. Major Update

```
Page uploaded → upload_to_milvus checks existing records
             ↓
         If record exists → Add current major to majors list
         If no record → Create new record with current major
```

## Configuration

No additional configuration is needed. The major-aware logic is enabled by default and works with existing settings:

```bash
# Existing configuration still applies
MAX_PAGES_PER_COLLEGE=400
CRAWLER_MAX_WORKERS=8
USE_PLAYWRIGHT_FALLBACK=1
```

## Testing

Run the test script to verify the logic:

```bash
python tests/test_major_aware_crawling.py
```

## Monitoring

The crawler provides detailed logging for major-aware decisions:

```
🔄 URL exists but missing major 'business', continuing crawl: https://example.edu/computer-science
✓ Updated majors for existing URL across all chunks (added 'business'): https://example.edu/computer-science
⚠️  Skipping URL with existing matching major across all chunks: https://example.edu/computer-science [computer_science]
```

## Performance Impact

### Positive Impact

- **Better content coverage**: More pages crawled for each major
- **Efficient updates**: Existing records updated instead of duplicated
- **Improved discovery**: Links found from pages across all majors

### Minimal Overhead

- **Additional database queries**: One query per URL to check major
- **Slight processing time**: Minor overhead for major checking
- **Memory usage**: Negligible increase

## Migration

### From Previous Version

- **Automatic**: No manual migration required
- **Backward compatible**: Existing data remains intact
- **Immediate benefits**: Better crawling starts immediately

### Data Integrity

- **No data loss**: Existing records are preserved
- **Major addition**: New majors are added to existing records
- **Consistent state**: Database remains consistent

## Best Practices

### 1. **Monitor Logs**

- Watch for "URL exists but missing major" messages
- Verify that major updates are happening correctly
- Check that appropriate URLs are being skipped

### 2. **Database Performance**

- Monitor query performance for major checking
- Consider indexing if needed for large datasets
- Watch for any timeout issues

### 3. **Crawling Efficiency**

- Adjust `MAX_PAGES_PER_COLLEGE` based on new behavior
- Monitor crawling speed and resource usage
- Balance between coverage and performance

## Future Enhancements

### Potential Improvements

- **Caching**: Cache major check results for better performance
- **Batch checking**: Check multiple URLs at once
- **Smart prioritization**: Prioritize URLs missing more majors
- **Analytics**: Track major coverage statistics

### Monitoring Features

- **Coverage reports**: Show which majors have good coverage
- **Missing content alerts**: Identify pages missing specific majors
- **Performance metrics**: Track crawling efficiency improvements
