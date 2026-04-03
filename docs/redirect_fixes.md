# Playwright Redirect Handling Fixes

This document describes the fixes implemented to address insufficient content issues when Playwright encounters redirects.

## Problem Statement

The original Playwright implementation had several issues with redirect handling:

1. **Final URL not captured**: The function returned the original URL instead of the final redirected URL
2. **Insufficient wait time**: No special handling for redirects that need extra processing time
3. **Poor link resolution**: Links were extracted using the original URL instead of the final redirected URL
4. **No redirect detection**: No mechanism to detect and adapt to redirect scenarios

## Solution Overview

### 1. Redirect Detection and URL Tracking

**File**: `multithreaded_crawler.py`
**Lines**: ~1777-1832

- Added `final_url` tracking to capture `page.url` after navigation
- Added `redirect_detected` flag to identify when redirects occur
- Updated return statement to use `final_url` instead of original URL
- Added `original_url` field to track the original URL when redirects happen

```python
# Navigate and capture final URL
response = page.goto(url, **strategy)
final_url = page.url

# Detect if a redirect occurred
if final_url != url:
    redirect_detected = True
    print(f"    🔄 Redirect detected: {url} -> {final_url}")
```

### 2. Enhanced Wait Strategies for Redirects

**File**: `multithreaded_crawler.py`
**Lines**: ~1810-1814, 1828-1832

- Added extra wait time specifically for redirected pages
- Uses `page.wait_for_load_state("networkidle")` with configurable timeout
- Fallback to sleep-based waiting if network idle detection fails

```python
# Give extra time for redirected content to load
if redirect_detected and PLAYWRIGHT_REDIRECT_DETECTION:
    try:
        page.wait_for_load_state("networkidle", timeout=PLAYWRIGHT_REDIRECT_EXTRA_WAIT)
    except Exception:
        time.sleep(PLAYWRIGHT_REDIRECT_EXTRA_WAIT // 1000)
```

### 3. Configuration Options

**File**: `config.py`
**Lines**: 330-335

Added new configuration options:

- `PLAYWRIGHT_REDIRECT_EXTRA_WAIT` (default: 5000ms): Extra wait time for redirected pages
- `PLAYWRIGHT_REDIRECT_DETECTION` (default: enabled): Enable/disable redirect-specific handling

```python
PLAYWRIGHT_REDIRECT_EXTRA_WAIT = int(
    os.getenv("PLAYWRIGHT_REDIRECT_EXTRA_WAIT", "5000")
)  # Extra wait time (ms) for redirected pages
PLAYWRIGHT_REDIRECT_DETECTION = (
    os.getenv("PLAYWRIGHT_REDIRECT_DETECTION", "1") == "1"
)  # Enable redirect detection and handling
```

### 4. Improved Link Resolution

**File**: `multithreaded_crawler.py`
**Lines**: ~2094-2096

- Updated link extraction to use `final_url` instead of original URL
- Ensures relative links are resolved correctly against the final destination

```python
links_local = self.extract_internal_links(
    soup_obj, final_url
)  # Use final URL for proper link resolution
```

### 5. Enhanced Return Data

**File**: `multithreaded_crawler.py`
**Lines**: 2154-2163

The Playwright function now returns additional metadata:

```python
return {
    "url": final_url,  # Use final URL after redirects
    "original_url": url if final_url != url else None,  # Track original if redirected
    "title": title_text,
    "content": chosen_content,
    "internal_links": internal_links,
    "word_count": word_count,
    "crawled_at": datetime.now().isoformat(),
    "redirect_detected": redirect_detected,
}
```

## Testing

A comprehensive test script is provided: `test_redirect_handling.py`

### Running Tests

```bash
cd college_ai
python test_redirect_handling.py
```

### Test URLs

The test includes:

- HTTP redirect test endpoints (httpbin.org)
- URL shorteners
- Common redirect patterns
- College URLs known to redirect

### Expected Output

```
🧪 Testing redirect handling for: http://httpbin.org/redirect/3
============================================================
    🔄 Redirect detected: http://httpbin.org/redirect/3 -> http://httpbin.org/get
✅ Success!
   Original URL: http://httpbin.org/redirect/3
   Final URL: http://httpbin.org/get
   Original URL (if redirected): http://httpbin.org/redirect/3
   Redirect detected: True
   ...
```

## Environment Variables

You can customize redirect handling behavior:

```bash
# Extra wait time for redirected pages (milliseconds)
export PLAYWRIGHT_REDIRECT_EXTRA_WAIT=7000

# Disable redirect detection if needed
export PLAYWRIGHT_REDIRECT_DETECTION=0
```

## Thread Safety

All redirect handling is thread-safe and compatible with the existing thread-safe architecture [[memory:6430750]]:

- Redirect detection uses local variables within each thread
- Configuration values are read-only
- No shared state modifications during redirect handling

## Benefits

1. **Accurate URL tracking**: Content is now correctly attributed to the final destination URL
2. **Improved content extraction**: More time for redirected pages to load completely
3. **Better link discovery**: Relative links resolve correctly against the final URL
4. **Enhanced debugging**: Clear logging when redirects are detected
5. **Configurable behavior**: Admins can tune redirect handling for their environment

## Backward Compatibility

- All changes are backward compatible
- Default settings maintain existing behavior when redirects don't occur
- No breaking changes to the API or data structure
- Existing configurations continue to work without modification

## Performance Impact

- Minimal impact when no redirects occur
- Additional 5-second wait only for pages that actually redirect
- Network idle detection prevents unnecessary waiting
- Configurable timeouts allow tuning for specific environments

## Bug Fixes

### Fixed: Variable Scoping Issue (v1.1)

**Problem**: `⚠️ Playwright fallback failed for <URL>: free variable 'final_url' referenced before assignment in enclosing scope`

**Root Cause**: The nested `extract_text_and_links` function was trying to access `final_url` from the outer scope, causing a Python scoping error.

**Solution**:

- Modified `extract_text_and_links` to accept `base_url` as a parameter
- Updated function calls to pass `final_url` as the `base_url` parameter
- Ensures proper variable scoping and eliminates the reference error

**Files Changed**:

- `multithreaded_crawler.py` lines ~2057, 2117-2118

## Future Enhancements

Potential improvements for future versions:

1. Redirect chain analysis (tracking multiple redirects)
2. Redirect caching to avoid repeated processing
3. Domain-specific redirect handling rules
4. Redirect performance metrics and monitoring
