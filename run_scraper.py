#!/usr/bin/env python3
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

from wikipedia_releases_scraper import WikipediaReleaseScraper

scraper = WikipediaReleaseScraper()
print("Starting Wikipedia scraper for 2026 albums...\n")

# Scrape all sources defined in the WIKIPEDIA_SOURCES
results = scraper.scrape_all_sources()

print("\nScraper completed!")
print("\nResults summary:")
for source_key, data in results.items():
    if isinstance(data, dict):
        print(f"  {source_key}: {data.get('items_added', 0)} items added, {data.get('error', 'OK')}")
    else:
        print(f"  {source_key}: {len(data) if isinstance(data, list) else 'Unknown'} items")
