# Playwright Fallback for Insufficient Content

This document describes the enhanced Playwright fallback functionality that ensures better content extraction when regular scraping encounters insufficient content.

## Overview

The crawler now includes comprehensive Playwright fallback mechanisms that automatically trigger when:

1. **Insufficient content is detected** - Pages with too few words or characters
2. **JavaScript-heavy pages** - Single Page Applications (SPAs) and dynamic content
3. **Failed initial scraping** - When regular requests fail completely
4. **Missing titles** - Pages that don't have proper titles
5. **No internal links found** - Pages that appear to be incomplete

## Configuration

### Environment Variables

```bash
# Enable/disable Playwright fallback (default: enabled)
USE_PLAYWRIGHT_FALLBACK=1

# Maximum concurrent Playwright instances (default: 3)
PLAYWRIGHT_MAX_CONCURRENCY=3

# Navigation timeout in milliseconds (default: 15000)
PLAYWRIGHT_NAV_TIMEOUT_MS=15000

# Aggressive fallback mode (default: disabled)
PLAYWRIGHT_AGGRESSIVE_FALLBACK=0
```

### Configuration Options

- **`USE_PLAYWRIGHT_FALLBACK`**: Set to `1` to enable Playwright fallback (default)
- **`PLAYWRIGHT_MAX_CONCURRENCY`**: Number of concurrent Playwright browser instances
- **`PLAYWRIGHT_NAV_TIMEOUT_MS`**: Timeout for page navigation in milliseconds
- **`PLAYWRIGHT_AGGRESSIVE_FALLBACK`**: When enabled, triggers Playwright for any insufficient content

## Fallback Triggers

### 1. Content Insufficiency Detection

Playwright fallback is triggered when content is insufficient based on:

```python
# Minimum content thresholds
MIN_CONTENT_LENGTH = 100  # Minimum characters
MIN_WORDS_PER_PAGE = 50   # Minimum words

# Triggers:
- Word count < MIN_WORDS_PER_PAGE
- Content length < MIN_CONTENT_LENGTH
- Very low content (< 10 words or < 50 characters)
- Significantly insufficient content (< half of minimum words)
```

### 2. JavaScript Detection

Pages are detected as JS-heavy based on:

- Framework markers (`__NEXT_DATA__`, `data-reactroot`, etc.)
- High script density (≥30 scripts or ≥25% script ratio)
- External JavaScript files (≥10)
- URL patterns (`/app/`, `/wp-json/`, etc.)

### 3. Failed Scraping

When initial scraping returns `None` (complete failure), Playwright fallback is automatically attempted.

### 4. Missing Content Indicators

- No title found
- No main content areas detected
- No internal links found (with additional heuristics)

## Implementation Details

### Enhanced Content Validation

```python
# Enhanced content insufficiency detection
content_insufficient = (
    len(cleaned_content.strip()) < max(1, min_chars) or
    word_count < max(1, min_words)
)

if content_insufficient:
    # Multiple fallback triggers
    if js_heavy:
        needs_pw = True
    elif word_count < 10 or len(cleaned_content.strip()) < 50:
        needs_pw = True
    elif not title_text.strip():
        needs_pw = True
    elif word_count < max(1, min_words // 2):
        needs_pw = True
    elif self.playwright_aggressive_fallback:
        needs_pw = True
```

### Asynchronous Processing

Playwright fallback jobs are processed asynchronously to avoid blocking the main crawling pipeline:

```python
# If this URL needs Playwright, offload the job
if page_data.get("needs_pw"):
    fut = pw_executor.submit(self._scrape_with_playwright, url)
    fut.add_done_callback(_merge_pw_result)
```

### Failed Scraping Fallback

When initial scraping fails completely:

```python
if not page_data:
    # Initial scraping failed - try Playwright fallback
    if self.playwright_enabled and sync_playwright is not None:
        pw_result = self._scrape_with_playwright(url)
        if pw_result:
            # Upload PW result and continue crawling
            self.upload_to_milvus(pw_result, college_name, major)
```

## Usage Examples

### Basic Usage

```python
from college_ai.scraping.crawler import MultithreadedCollegeCrawler

# Initialize crawler (Playwright fallback enabled by default)
crawler = MultithreadedCollegeCrawler()

# Crawl a college site
college = {"name": "Test University", "url": "https://example.edu", "major": "computer_science"}
result = crawler.crawl_college_site(college)
```

### Testing Fallback

```python
# Test Playwright fallback functionality
python tests/test_playwright_fallback.py
```

### Configuration Examples

```bash
# Conservative fallback (default)
USE_PLAYWRIGHT_FALLBACK=1
PLAYWRIGHT_AGGRESSIVE_FALLBACK=0

# Aggressive fallback (use Playwright more frequently)
USE_PLAYWRIGHT_FALLBACK=1
PLAYWRIGHT_AGGRESSIVE_FALLBACK=1

# Disable Playwright fallback
USE_PLAYWRIGHT_FALLBACK=0
```

## Performance Considerations

### Resource Usage

- **Memory**: Each Playwright browser instance uses ~50-100MB RAM
- **CPU**: Browser rendering is CPU-intensive
- **Network**: Playwright loads full page resources

### Optimization

- **Concurrency limits**: Default 3 concurrent instances
- **Resource blocking**: Images, fonts, and media are blocked
- **Timeout controls**: Configurable navigation timeouts
- **Browser reuse**: Shared browser instances per proxy

### Monitoring

The crawler provides detailed logging for Playwright fallback:

```
🔄 Triggering Playwright fallback (JS-heavy page)
🔄 Triggering Playwright fallback (very low content)
🔄 Triggering Playwright fallback (no title)
✅ Playwright fallback successful for https://example.com (words=150, chars=1200)
⚠️  Playwright fallback still insufficient for https://example.com (words=5, chars=50)
```

## Troubleshooting

### Common Issues

1. **Playwright not installed**:

   ```bash
   pip install playwright
   playwright install chromium
   ```

2. **High memory usage**:

   - Reduce `PLAYWRIGHT_MAX_CONCURRENCY`
   - Monitor system resources

3. **Slow performance**:

   - Increase `PLAYWRIGHT_NAV_TIMEOUT_MS`
   - Check network connectivity

4. **Failed fallbacks**:
   - Check if target site blocks automated browsers
   - Verify proxy settings if using proxies

### Debug Mode

Enable detailed logging by setting environment variables:

```bash
LOG_LEVEL=DEBUG
USE_PLAYWRIGHT_FALLBACK=1
```

## Best Practices

1. **Start conservative**: Use default settings first
2. **Monitor performance**: Watch for memory/CPU usage
3. **Test thoroughly**: Use the test script to validate fallback behavior
4. **Adjust gradually**: Modify settings based on specific site requirements
5. **Use aggressive mode sparingly**: Only when needed for problematic sites

## Future Enhancements

- **Machine learning detection**: AI-powered content sufficiency assessment
- **Site-specific profiles**: Custom fallback rules per domain
- **Progressive enhancement**: Gradual fallback strategies
- **Performance optimization**: Faster browser startup and rendering
