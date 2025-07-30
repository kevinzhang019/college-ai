"""
Multithreaded College Site Crawler
Reads college URLs from CSV files and performs multithreaded crawling of each site.
Uses BeautifulSoup to find internal links and uploads each page directly to Milvus.
"""

import os
import sys
import csv
import glob
import time
import uuid
import threading
import queue
import random
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional, Set
from datetime import datetime
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility
import openai

# Add parent directory to path for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from preference_scraper.utils.openai_embed import get_embedding
from preference_scraper.utils.text_cleaner import clean_text
from preference_scraper.crawlers.config import *

class MultithreadedCollegeCrawler:
    """Multithreaded crawler that crawls college websites and uploads directly to Milvus."""
    
    def __init__(self, delay: float = None, max_workers: int = None):
        """
        Initialize the crawler.
        
        Args:
            delay: Delay between requests to be respectful (uses config if None)
            max_workers: Number of worker threads per college (uses config if None)
        """
        self.delay = delay or CRAWLER_DELAY
        self.max_workers = max_workers or CRAWLER_MAX_WORKERS
        self.colleges_dir = os.path.join(os.path.dirname(__file__), 'colleges')
        
        # Ensure colleges directory exists
        os.makedirs(self.colleges_dir, exist_ok=True)
        
        # Initialize session for requests with realistic headers
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0'
        })
        
        # Anti-bot detection settings
        self.min_delay = max(0.5, self.delay * 0.5)  # Minimum delay
        self.max_delay = self.delay * 2.0  # Maximum delay for randomization
        self.max_retries = 3
        self.robots_cache = {}  # Cache robots.txt results
        
        # Thread-safe sets for preventing duplicates
        self.crawled_urls = set()
        self.discovered_urls = set()
        self.uploaded_urls = set()  # Track URLs already uploaded to Milvus
        self.lock = threading.Lock()
        
        # Milvus connection
        self.connect_milvus()
        self.collection = self.get_or_create_collection()
        
        # Crawling statistics
        self.stats = {
            'total_pages_crawled': 0,
            'total_pages_uploaded': 0,
            'total_errors': 0,
            'colleges_processed': 0,
            'duplicate_urls_skipped': 0
        }
    
    def connect_milvus(self):
        """Connect to Milvus database."""
        try:
            connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
            print("✓ Connected to Milvus")
        except Exception as e:
            print(f"✗ Failed to connect to Milvus: {e}")
            raise
    
    def get_or_create_collection(self):
        """Get or create the Milvus collection."""
        # Define Milvus schema for college pages
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, auto_id=False, max_length=36),
            FieldSchema(name="college_name", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="url", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=MAX_TITLE_LENGTH),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=MAX_CONTENT_LENGTH),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
            FieldSchema(name="crawled_at", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="major", dtype=DataType.VARCHAR, max_length=64)
        ]
        
        schema = CollectionSchema(fields, description="College pages with embeddings")
        
        if utility.has_collection(MILVUS_COLLECTION_NAME):
            return Collection(MILVUS_COLLECTION_NAME)
        return Collection(MILVUS_COLLECTION_NAME, schema)
    
    def read_csv_files(self) -> Dict[str, List[Dict[str, str]]]:
        """
        Read all CSV files in the colleges directory and organize by major.
        
        Returns:
            Dictionary mapping major names to lists of college data
        """
        majors_data = {}
        
        # Find all CSV files in colleges directory
        csv_pattern = os.path.join(self.colleges_dir, '*.csv')
        csv_files = glob.glob(csv_pattern)
        
        if not csv_files:
            print(f"No CSV files found in {self.colleges_dir}")
            # Create a sample CSV file for demonstration
            self.create_sample_csv_files()
            csv_files = glob.glob(csv_pattern)
        
        for csv_file in csv_files:
            # Extract major name from filename (e.g., 'business.csv' -> 'business')
            major_name = os.path.splitext(os.path.basename(csv_file))[0]
            
            print(f"Reading {major_name} colleges from {csv_file}")
            
            colleges = []
            try:
                with open(csv_file, 'r', encoding='utf-8', newline='') as f:
                    # Try to detect if file has headers
                    sample = f.read(1024)
                    f.seek(0)
                    
                    # Check if file is empty or only whitespace
                    if not sample.strip():
                        print(f"Warning: {csv_file} is empty")
                        continue
                    
                    reader = csv.DictReader(f)
                    
                    # Handle different possible column names
                    fieldnames = reader.fieldnames
                    if not fieldnames:
                        print(f"Warning: {csv_file} has no headers")
                        continue
                    
                    # Map common column variations
                    name_col = None
                    url_col = None
                    
                    for field in fieldnames:
                        field_lower = field.lower().strip()
                        if field_lower in ['name', 'college_name', 'university_name', 'school_name']:
                            name_col = field
                        elif field_lower in ['url', 'website', 'link', 'college_url', 'university_url']:
                            url_col = field
                    
                    if not name_col or not url_col:
                        print(f"Warning: {csv_file} missing required columns (name/url)")
                        print(f"Available columns: {fieldnames}")
                        continue
                    
                    for row in reader:
                        name = row.get(name_col, '').strip()
                        url = row.get(url_col, '').strip()
                        
                        if name and url:
                            # Ensure URL has proper protocol
                            if not url.startswith(('http://', 'https://')):
                                url = 'https://' + url
                            
                            colleges.append({
                                'name': name,
                                'url': url,
                                'major': major_name
                            })
                
                if colleges:
                    majors_data[major_name] = colleges
                    print(f"✓ Loaded {len(colleges)} colleges for {major_name}")
                else:
                    print(f"Warning: No valid college data found in {csv_file}")
                    
            except Exception as e:
                print(f"Error reading {csv_file}: {e}")
        
        return majors_data
    
    def create_sample_csv_files(self):
        """Create sample CSV files for demonstration purposes."""
        print("Creating sample CSV files for demonstration...")
        
        sample_data = {
            'business.csv': [
                {'name': 'Harvard Business School', 'url': 'https://www.hbs.edu/'},
                {'name': 'Stanford Graduate School of Business', 'url': 'https://www.gsb.stanford.edu/'},
                {'name': 'Wharton School', 'url': 'https://www.wharton.upenn.edu/'},
            ],
            'computer_science.csv': [
                {'name': 'MIT EECS', 'url': 'https://www.eecs.mit.edu/'},
                {'name': 'Stanford CS', 'url': 'https://cs.stanford.edu/'},
                {'name': 'Carnegie Mellon SCS', 'url': 'https://www.cs.cmu.edu/'},
            ]
        }
        
        for filename, colleges in sample_data.items():
            csv_path = os.path.join(self.colleges_dir, filename)
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=['name', 'url'])
                writer.writeheader()
                writer.writerows(colleges)
            print(f"Created sample file: {csv_path}")
    
    def is_internal_link(self, url: str, base_domain: str) -> bool:
        """Check if a URL is an internal link to the same domain."""
        try:
            parsed_url = urlparse(url)
            parsed_base = urlparse(base_domain)
            
            # Must be from the same domain
            if parsed_url.netloc and parsed_url.netloc != parsed_base.netloc:
                return False
            
            # Skip certain file types
            if any(url.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
                return False
            
            # Skip certain paths
            if any(skip_path in url.lower() for skip_path in SKIP_PATHS):
                return False
            
            return True
            
        except Exception:
            return False
    
    def extract_internal_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        """Extract all internal links from a BeautifulSoup object."""
        links = []
        base_domain = urlparse(base_url).netloc
        
        for link in soup.find_all('a', href=True):
            href = link.get('href')
            if href:
                # Convert relative URLs to absolute
                absolute_url = urljoin(base_url, href)
                
                # Check if it's an internal link
                if self.is_internal_link(absolute_url, base_domain):
                    links.append(absolute_url)
        
        return list(set(links))  # Remove duplicates
    
    def scrape_page(self, url: str) -> Optional[Dict[str, Any]]:
        """Scrape a single page and return structured data."""
        try:
            print(f"    Crawling: {url}")
            
            # Fetch the page
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            # Parse with BeautifulSoup
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract title
            title = soup.find('title')
            title_text = title.get_text(strip=True) if title else ""
            
            # Extract main content
            # Remove script, style, nav, footer, header elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()
            
            # Try to find main content areas
            main_content = ""
            main_selectors = [
                'main', 'article', '[role="main"]', '.main-content', 
                '.content', '#content', '.post-content', '.entry-content'
            ]
            
            for selector in main_selectors:
                main_element = soup.select_one(selector)
                if main_element:
                    main_content = main_element.get_text(separator=' ', strip=True)
                    break
            
            # Fallback to body if no main content found
            if not main_content:
                body = soup.find('body')
                if body:
                    main_content = body.get_text(separator=' ', strip=True)
            
            # Clean the content
            cleaned_content = clean_text(main_content)
            
            # Extract internal links
            internal_links = self.extract_internal_links(soup, url)
            
            return {
                'url': url,
                'title': title_text,
                'content': cleaned_content,
                'internal_links': internal_links,
                'word_count': len(cleaned_content.split()),
                'crawled_at': datetime.now().isoformat()
            }
            
        except Exception as e:
            print(f"    ✗ Error scraping {url}: {e}")
            return None
    
    def upload_to_milvus(self, page_data: Dict[str, Any], college_name: str, major: str) -> bool:
        """Upload a single page to Milvus with embedding."""
        try:
            # Check if URL has already been uploaded (duplicate prevention)
            with self.lock:
                if page_data['url'] in self.uploaded_urls:
                    print(f"    ⚠️  Skipping duplicate URL: {page_data['url']}")
                    self.stats['duplicate_urls_skipped'] += 1
                    return False
                
                # Add to uploaded URLs set
                self.uploaded_urls.add(page_data['url'])
            
            # Generate embedding for the content
            content_for_embedding = f"{page_data['title']} {page_data['content']}"
            embedding = get_embedding(content_for_embedding)
            
            if not embedding:
                print(f"    ✗ Failed to generate embedding for {page_data['url']}")
                return False
            
            # Prepare data for Milvus
            data = [
                {
                    "id": str(uuid.uuid4()),
                    "college_name": college_name,
                    "url": page_data['url'],
                    "title": page_data['title'][:MAX_TITLE_LENGTH-1],  # Limit to config length
                    "content": page_data['content'][:MAX_CONTENT_LENGTH-1],  # Limit to config length
                    "embedding": embedding,
                    "crawled_at": page_data['crawled_at'],
                    "major": major
                }
            ]
            
            # Insert into Milvus
            self.collection.insert(data)
            
            with self.lock:
                self.stats['total_pages_uploaded'] += 1
            
            print(f"    ✓ Uploaded to Milvus: {page_data['url']}")
            return True
            
        except Exception as e:
            print(f"    ✗ Error uploading to Milvus: {e}")
            with self.lock:
                self.stats['total_errors'] += 1
            return False
    
    def crawl_college_site(self, college: Dict[str, str], max_pages: int = None) -> Dict[str, Any]:
        """Crawl a single college website using multiple threads."""
        college_name = college['name']
        base_url = college['url']
        major = college['major']
        max_pages = max_pages or CRAWLER_MAX_PAGES_PER_COLLEGE
        
        print(f"\n=== Crawling {college_name} ({major}) ===")
        print(f"Base URL: {base_url}")
        
        # Reset crawling state for this college
        with self.lock:
            self.crawled_urls.clear()
            self.discovered_urls.clear()
        
        # Start with the base URL
        urls_to_crawl = [base_url]
        self.discovered_urls.add(base_url)
        
        pages_crawled = 0
        pages_uploaded = 0
        
        # Use ThreadPoolExecutor for multithreaded crawling
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while urls_to_crawl and pages_crawled < max_pages:
                # Take a batch of URLs to crawl
                current_batch = urls_to_crawl[:self.max_workers]
                urls_to_crawl = urls_to_crawl[self.max_workers:]
                
                print(f"  Crawling batch of {len(current_batch)} URLs...")
                
                # Submit crawling tasks
                future_to_url = {
                    executor.submit(self.scrape_page, url): url 
                    for url in current_batch 
                    if url not in self.crawled_urls
                }
                
                # Process completed tasks
                for future in as_completed(future_to_url):
                    url = future_to_url[future]
                    
                    try:
                        page_data = future.result()
                        
                        if page_data:
                            pages_crawled += 1
                            
                            # Upload to Milvus immediately
                            if self.upload_to_milvus(page_data, college_name, major):
                                pages_uploaded += 1
                            
                            # Add new internal links to crawl queue (using sets for duplicate prevention)
                            for link in page_data.get('internal_links', []):
                                with self.lock:
                                    if (link not in self.crawled_urls and 
                                        link not in self.discovered_urls and 
                                        len(urls_to_crawl) + pages_crawled < max_pages):
                                        urls_to_crawl.append(link)
                                        self.discovered_urls.add(link)
                            
                            with self.lock:
                                self.stats['total_pages_crawled'] += 1
                        
                        # Mark as crawled
                        with self.lock:
                            self.crawled_urls.add(url)
                        
                        # Be respectful
                        time.sleep(self.delay)
                        
                    except Exception as e:
                        print(f"    ✗ Error processing {url}: {e}")
                        with self.lock:
                            self.stats['total_errors'] += 1
                            self.crawled_urls.add(url)
        
        # Print summary
        print(f"\n✓ Completed crawling {college_name}")
        print(f"  Pages crawled: {pages_crawled}")
        print(f"  Pages uploaded to Milvus: {pages_uploaded}")
        print(f"  Unique URLs discovered: {len(self.discovered_urls)}")
        
        return {
            'college_name': college_name,
            'major': major,
            'base_url': base_url,
            'pages_crawled': pages_crawled,
            'pages_uploaded': pages_uploaded,
            'urls_discovered': len(self.discovered_urls)
        }
    
    def crawl_all_colleges(self, majors_data: Dict[str, List[Dict[str, str]]], 
                          max_pages_per_college: int = 50):
        """
        Crawl all colleges from all majors and upload directly to Milvus.
        
        Args:
            majors_data: Dictionary mapping majors to college lists
            max_pages_per_college: Maximum pages to crawl per college
        """
        print("=== MULTITHREADED COLLEGE CRAWLING PIPELINE ===")
        print(f"Configuration:")
        print(f"  - Max workers per college: {self.max_workers}")
        print(f"  - Max pages per college: {max_pages_per_college}")
        print(f"  - Delay between requests: {self.delay}s")
        print(f"  - Direct upload to Milvus: ✓")
        
        total_colleges = sum(len(colleges) for colleges in majors_data.values())
        college_count = 0
        
        for major, colleges in majors_data.items():
            print(f"\n=== Processing {major.upper()} ({len(colleges)} colleges) ===")
            
            major_stats = {
                'total_pages_crawled': 0,
                'total_pages_uploaded': 0,
                'total_errors': 0
            }
            
            for college in colleges:
                college_count += 1
                print(f"\n--- [{college_count}/{total_colleges}] Processing {college['name']} ---")
                
                try:
                    # Crawl the college site
                    college_result = self.crawl_college_site(college, max_pages_per_college)
                    
                    # Update major statistics
                    major_stats['total_pages_crawled'] += college_result['pages_crawled']
                    major_stats['total_pages_uploaded'] += college_result['pages_uploaded']
                    
                    with self.lock:
                        self.stats['colleges_processed'] += 1
                    
                except Exception as e:
                    print(f"  ✗ Error processing {college['name']}: {e}")
                    with self.lock:
                        self.stats['total_errors'] += 1
            
            # Print major summary
            print(f"\n{major.upper()} Summary:")
            print(f"  📄 Pages crawled: {major_stats['total_pages_crawled']}")
            print(f"  📤 Pages uploaded: {major_stats['total_pages_uploaded']}")
            print(f"  ✗ Errors: {major_stats['total_errors']}")
        
        # Print overall summary
        print(f"\n=== FINAL CRAWLING SUMMARY ===")
        print(f"Total colleges processed: {self.stats['colleges_processed']}")
        print(f"Total pages crawled: {self.stats['total_pages_crawled']}")
        print(f"Total pages uploaded to Milvus: {self.stats['total_pages_uploaded']}")
        print(f"Duplicate URLs skipped: {self.stats['duplicate_urls_skipped']}")
        print(f"Total errors: {self.stats['total_errors']}")
        print(f"All data is now available in Milvus for vector search!")
    
    def run_full_crawling_pipeline(self, max_pages_per_college: int = None):
        """
        Run the complete multithreaded crawling pipeline.
        
        Args:
            max_pages_per_college: Maximum pages to crawl per college (uses config if None)
        """
        max_pages_per_college = max_pages_per_college or MAX_PAGES_PER_COLLEGE
        
        # Step 1: Read CSV files
        print("\n1. Reading CSV files...")
        majors_data = self.read_csv_files()
        
        if not majors_data:
            print("No college data found. Please check your CSV files.")
            return
        
        # Step 2: Crawl all colleges and upload to Milvus
        print("\n2. Starting multithreaded crawling and uploading to Milvus...")
        self.crawl_all_colleges(majors_data, max_pages_per_college)
        
        print(f"\n🎉 Multithreaded crawling completed successfully!")
        print(f"📊 All pages have been uploaded to Milvus for vector search!")


def main():
    """Main function to run the multithreaded crawler."""
    # Initialize crawler with config settings
    crawler = MultithreadedCollegeCrawler()
    
    # Run the full pipeline
    crawler.run_full_crawling_pipeline()


if __name__ == "__main__":
    main() 