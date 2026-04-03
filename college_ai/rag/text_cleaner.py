"""
Text Cleaning Utility
Provides functionality to clean and preprocess text content for embedding and storage.
"""

import re
import html
from typing import Optional
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def clean_text(text: str, max_length: Optional[int] = None) -> str:
    """
    Clean and preprocess text content for embedding and storage.
    
    Args:
        text: Raw text content to clean
        max_length: Maximum length to truncate to (optional)
        
    Returns:
        Cleaned text string
    """
    if not text:
        return ""
    
    # Convert to string if not already
    text = str(text)
    
    # Decode HTML entities
    text = html.unescape(text)
    
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Remove script and style content
    text = re.sub(r'<script.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    
    # Remove special characters but keep basic punctuation
    text = re.sub(r'[^\w\s\.\,\!\?\;\:\-\(\)\[\]\"\']+', ' ', text)
    
    # Remove URLs
    text = re.sub(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', '', text)
    
    # Remove email addresses
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '', text)
    
    # Remove phone numbers (basic pattern)
    text = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '', text)
    
    # Remove excessive punctuation
    text = re.sub(r'[.]{3,}', '...', text)
    text = re.sub(r'[!]{2,}', '!', text)
    text = re.sub(r'[?]{2,}', '?', text)
    
    # Clean up whitespace again
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    
    # Truncate if max_length specified
    if max_length and len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0]  # Truncate at word boundary
        if text:
            text += "..."
    
    return text

def extract_title_from_html(html_content: str) -> str:
    """
    Extract title from HTML content.
    
    Args:
        html_content: Raw HTML content
        
    Returns:
        Extracted title or empty string
    """
    if not html_content:
        return ""
    
    # Try to find title tag
    title_match = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = title_match.group(1)
        return clean_text(title, max_length=200)
    
    # Try to find h1 tag as fallback
    h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html_content, re.IGNORECASE | re.DOTALL)
    if h1_match:
        title = h1_match.group(1)
        return clean_text(title, max_length=200)
    
    return ""

def extract_main_content(html_content: str) -> str:
    """
    Extract main content from HTML, filtering out navigation, headers, footers, etc.
    
    Args:
        html_content: Raw HTML content
        
    Returns:
        Extracted main content
    """
    if not html_content:
        return ""
    
    # Remove script and style tags first
    content = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<style.*?</style>', '', content, flags=re.DOTALL | re.IGNORECASE)
    
    # Remove common navigation and footer elements
    content = re.sub(r'<nav.*?</nav>', '', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<header.*?</header>', '', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<footer.*?</footer>', '', content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<aside.*?</aside>', '', content, flags=re.DOTALL | re.IGNORECASE)
    
    # Remove elements with common navigation/menu classes and IDs
    nav_patterns = [
        r'<[^>]+(?:class|id)="[^"]*(?:nav|menu|sidebar|header|footer|breadcrumb)[^"]*"[^>]*>.*?</[^>]+>',
        r'<[^>]+(?:class|id)="[^"]*(?:social|share|comment|advertisement|ad)[^"]*"[^>]*>.*?</[^>]+>'
    ]
    
    for pattern in nav_patterns:
        content = re.sub(pattern, '', content, flags=re.DOTALL | re.IGNORECASE)
    
    # Try to find main content area
    main_patterns = [
        r'<main[^>]*>(.*?)</main>',
        r'<article[^>]*>(.*?)</article>',
        r'<div[^>]+(?:class|id)="[^"]*(?:main|content|body|article)[^"]*"[^>]*>(.*?)</div>',
        r'<section[^>]+(?:class|id)="[^"]*(?:main|content|body)[^"]*"[^>]*>(.*?)</section>'
    ]
    
    for pattern in main_patterns:
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            main_content = match.group(1)
            return clean_text(main_content)
    
    # If no main content found, clean the entire content
    return clean_text(content)

def is_valid_content(text: str, min_length: int = 50) -> bool:
    """
    Check if text content is valid and substantial enough to process.
    
    Args:
        text: Text content to validate
        min_length: Minimum length requirement
        
    Returns:
        True if content is valid, False otherwise
    """
    if not text or not text.strip():
        return False
    
    cleaned = clean_text(text)
    
    # Check minimum length
    if len(cleaned) < min_length:
        return False
    
    # Check if it's mostly meaningful content (not just numbers/symbols)
    word_count = len(re.findall(r'\b[a-zA-Z]{3,}\b', cleaned))
    if word_count < 5:  # At least 5 meaningful words
        return False
    
    return True

def remove_duplicate_sentences(text: str) -> str:
    """
    Remove duplicate sentences from text content.
    
    Args:
        text: Input text
        
    Returns:
        Text with duplicate sentences removed
    """
    if not text:
        return ""
    
    # Split into sentences
    sentences = re.split(r'[.!?]+', text)
    
    # Clean and deduplicate sentences
    unique_sentences = []
    seen_sentences = set()
    
    for sentence in sentences:
        cleaned_sentence = sentence.strip()
        if cleaned_sentence and len(cleaned_sentence) > 10:
            # Normalize for comparison (lowercase, remove extra spaces)
            normalized = re.sub(r'\s+', ' ', cleaned_sentence.lower())
            if normalized not in seen_sentences:
                seen_sentences.add(normalized)
                unique_sentences.append(cleaned_sentence)
    
    return '. '.join(unique_sentences) + '.' if unique_sentences else ""

def extract_keywords(text: str, max_keywords: int = 20) -> list:
    """
    Extract important keywords from text content.
    
    Args:
        text: Input text
        max_keywords: Maximum number of keywords to return
        
    Returns:
        List of extracted keywords
    """
    if not text:
        return []
    
    # Clean text and convert to lowercase
    cleaned_text = clean_text(text).lower()
    
    # Common stop words to exclude
    stop_words = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with',
        'by', 'from', 'up', 'about', 'into', 'through', 'during', 'before', 'after',
        'above', 'below', 'between', 'among', 'is', 'are', 'was', 'were', 'be', 'been',
        'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'must', 'can', 'this', 'that', 'these', 'those'
    }
    
    # Extract words (3+ characters)
    words = re.findall(r'\b[a-zA-Z]{3,}\b', cleaned_text)
    
    # Filter out stop words and count frequency
    word_freq = {}
    for word in words:
        if word not in stop_words:
            word_freq[word] = word_freq.get(word, 0) + 1
    
    # Sort by frequency and return top keywords
    sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
    return [word for word, freq in sorted_words[:max_keywords]]
