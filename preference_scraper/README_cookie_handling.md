# Enhanced Cookie Handling for Playwright

This document describes the comprehensive cookie handling improvements made to the Playwright fallback system.

## Overview

The Playwright implementation now includes advanced cookie management capabilities that go far beyond basic cookie banner acceptance. These improvements ensure better success rates when crawling sites with cookie walls, authentication requirements, and anti-bot measures.

## Previous Limitations

### **What the Original Implementation Did:**

- ✅ Basic cookie banner acceptance (limited selectors)
- ✅ Cookie element removal from HTML
- ❌ **No cookie persistence** between requests
- ❌ **No session management**
- ❌ **Limited banner coverage**
- ❌ **No authentication support**

### **What the Enhanced Implementation Does:**

- ✅ **Comprehensive cookie banner acceptance** (50+ selectors)
- ✅ **Cookie persistence** across requests
- ✅ **Session management** per domain
- ✅ **JavaScript-based fallback** for stubborn banners
- ✅ **Multi-language support**
- ✅ **Authentication cookie handling**
- ✅ **Configurable behavior**

## Key Improvements

### 1. **Cookie Persistence**

Cookies are now saved and reused across requests for the same domain:

```python
# Load cookies for this domain if available
if self.playwright_cookie_persistence:
    storage_state = self._load_cookies(netloc)
    if storage_state:
        context_kwargs["storage_state"] = storage_state
```

**Benefits:**

- **Session continuity**: Maintains login states
- **Reduced banner clicks**: Accept once, reuse cookies
- **Better success rates**: Sites recognize returning "users"
- **Faster crawling**: No repeated cookie acceptance

### 2. **Comprehensive Banner Acceptance**

Enhanced selector coverage for 50+ common cookie banner frameworks:

```python
cookie_selectors = [
    # OneTrust
    "#onetrust-accept-btn-handler",
    "#onetrust-banner-sdk .accept-btn",

    # Cookiebot
    "#CybotCookiebotDialogBodyLevelButtonAccept",
    ".CybotCookiebotDialogBodyButton",

    # GDPR/Consent frameworks
    "#sp-cc-accept",
    ".sp_choice_type_11",

    # Generic patterns
    'button:has-text("Accept all")',
    '[data-testid*="accept"]',
    '[class*="accept"]',

    # Multi-language support
    'button:has-text("Akzeptieren")',  # German
    'button:has-text("Accepter")',     # French
    'button:has-text("Aceptar")',      # Spanish
]
```

### 3. **JavaScript Fallback**

When CSS selectors fail, JavaScript-based acceptance is attempted:

```javascript
// Remove cookie banners
document.querySelectorAll('[id*="cookie"], [class*="cookie"]').forEach((el) => {
  if (el.style.display !== "none") {
    el.style.display = "none";
    el.remove();
  }
});

// Accept cookies via JavaScript functions
window.acceptAllCookies && window.acceptAllCookies();
window.acceptCookies && window.acceptCookies();
```

### 4. **Multi-Strategy Approach**

Each selector is tried with multiple strategies:

```python
strategies = [
    lambda: page.locator(sel).first,
    lambda: page.locator(sel).nth(0),
    lambda: page.locator(f"{sel}:visible").first,
]
```

## Configuration

### Environment Variables

```bash
# Enable/disable cookie persistence (default: enabled)
PLAYWRIGHT_COOKIE_PERSISTENCE=1

# Other Playwright settings
USE_PLAYWRIGHT_FALLBACK=1
PLAYWRIGHT_MAX_CONCURRENCY=3
PLAYWRIGHT_NAV_TIMEOUT_MS=15000
```

### Configuration Options

- **`PLAYWRIGHT_COOKIE_PERSISTENCE`**: Set to `1` to enable cookie persistence (default)
- **`USE_PLAYWRIGHT_FALLBACK`**: Enable Playwright fallback (required for cookie handling)
- **`PLAYWRIGHT_MAX_CONCURRENCY`**: Number of concurrent browser instances
- **`PLAYWRIGHT_NAV_TIMEOUT_MS`**: Navigation timeout in milliseconds

## Implementation Details

### Cookie Storage

Cookies are stored per domain in JSON format:

```json
{
  "cookies": [
    {
      "name": "session_id",
      "value": "abc123",
      "domain": "example.com",
      "path": "/"
    }
  ],
  "origins": []
}
```

**Storage Location**: `preference_scraper/crawlers/playwright_cookies/`

### Cookie Management Functions

#### `_get_cookie_storage_path(netloc)`

Generates safe filenames for cookie storage.

#### `_load_cookies(netloc)`

Loads cookies for a specific domain.

#### `_save_cookies(netloc, storage_state)`

Saves cookies for a specific domain.

#### `_try_accept_cookies(page)`

Enhanced cookie banner acceptance with comprehensive coverage.

## Usage Examples

### Basic Usage

```python
from preference_scraper.crawlers.multithreaded_crawler import MultithreadedCollegeCrawler

# Initialize crawler (cookie persistence enabled by default)
crawler = MultithreadedCollegeCrawler()

# Crawl a site with cookie handling
result = crawler._scrape_with_playwright("https://example.com")
```

### Configuration Examples

```bash
# Enable cookie persistence (default)
PLAYWRIGHT_COOKIE_PERSISTENCE=1

# Disable cookie persistence
PLAYWRIGHT_COOKIE_PERSISTENCE=0

# Conservative settings
PLAYWRIGHT_COOKIE_PERSISTENCE=1
PLAYWRIGHT_MAX_CONCURRENCY=2

# Aggressive settings
PLAYWRIGHT_COOKIE_PERSISTENCE=1
PLAYWRIGHT_MAX_CONCURRENCY=5
```

## Testing

### Test Cookie Handling

```bash
python preference_scraper/test_cookie_handling.py
```

### Test Individual Functions

```python
# Test cookie storage
crawler = MultithreadedCollegeCrawler()
path = crawler._get_cookie_storage_path("example.com")
print(f"Cookie path: {path}")

# Test cookie loading/saving
test_state = {"cookies": [], "origins": []}
crawler._save_cookies("example.com", test_state)
loaded = crawler._load_cookies("example.com")
```

## Performance Impact

### Positive Impact

- **Better success rates**: Sites with cookie walls now accessible
- **Faster subsequent requests**: No repeated banner acceptance
- **Session continuity**: Maintains authentication states
- **Reduced blocking**: Sites recognize returning "users"

### Resource Usage

- **Storage**: ~1-10KB per domain for cookie files
- **Memory**: Minimal increase for cookie storage
- **Processing**: Slight overhead for cookie management
- **Network**: Reduced requests due to session reuse

## Monitoring

### Log Messages

The crawler provides detailed logging for cookie operations:

```
🍪 Loaded cookies for example.com
🍪 Accepted cookies using selector: #onetrust-accept-btn-handler
💾 Saved cookies for example.com
⚠️  Failed to save cookies for example.com: Permission denied
```

### Cookie Files

Monitor the cookie storage directory:

```bash
ls -la preference_scraper/crawlers/playwright_cookies/
# example_com_cookies.json
# bbc_com_cookies.json
# harvard_edu_cookies.json
```

## Troubleshooting

### Common Issues

1. **Cookie files not being saved**:

   - Check `PLAYWRIGHT_COOKIE_PERSISTENCE=1`
   - Verify write permissions to cookie directory
   - Check for disk space

2. **Cookie banners not being accepted**:

   - Check browser console for JavaScript errors
   - Verify selector coverage for specific site
   - Try JavaScript fallback mode

3. **Performance issues**:
   - Reduce `PLAYWRIGHT_MAX_CONCURRENCY`
   - Monitor cookie file sizes
   - Clean up old cookie files periodically

### Debug Mode

Enable detailed logging:

```bash
LOG_LEVEL=DEBUG
PLAYWRIGHT_COOKIE_PERSISTENCE=1
```

## Best Practices

### 1. **Cookie Management**

- **Regular cleanup**: Remove old cookie files periodically
- **Monitor storage**: Watch cookie file sizes and counts
- **Test persistence**: Verify cookies are being reused

### 2. **Performance Optimization**

- **Limit concurrency**: Balance between speed and resource usage
- **Monitor memory**: Watch for memory leaks with persistent contexts
- **Timeout settings**: Adjust timeouts based on site responsiveness

### 3. **Security Considerations**

- **Cookie isolation**: Cookies are stored per domain
- **No sensitive data**: Only session/functional cookies are stored
- **File permissions**: Ensure cookie directory has appropriate permissions

## Advanced Features

### 1. **Multi-Language Support**

The system handles cookie banners in multiple languages:

- English: "Accept all", "I agree"
- German: "Akzeptieren"
- French: "Accepter"
- Spanish: "Aceptar"
- Italian: "Accetta"

### 2. **Framework-Specific Handling**

Specialized handling for common frameworks:

- **OneTrust**: Comprehensive selector coverage
- **Cookiebot**: Multiple acceptance strategies
- **GDPR frameworks**: Generic and specific selectors
- **Custom frameworks**: JavaScript fallback

### 3. **Session Management**

- **Domain isolation**: Cookies stored per domain
- **Context reuse**: Browser contexts with persistent storage
- **State preservation**: Maintains login and preference states

## Future Enhancements

### Potential Improvements

- **Cookie expiration**: Automatic cleanup of expired cookies
- **Encryption**: Encrypt stored cookie files
- **Compression**: Compress cookie storage files
- **Analytics**: Track cookie acceptance success rates
- **Machine learning**: Learn optimal acceptance strategies per site

### Monitoring Features

- **Success rate tracking**: Monitor cookie acceptance effectiveness
- **Performance metrics**: Track cookie-related performance impact
- **Storage analytics**: Monitor cookie storage usage patterns
- **Error reporting**: Detailed error reporting for cookie operations
