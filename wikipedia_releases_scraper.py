#!/usr/bin/env python3
"""
Wikipedia Album Release Scraper

Scrapes upcoming album releases from Wikipedia pages for various genres and regions.
Parses release tables and stores information in the database.
"""
import requests
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import re

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Wikipedia URLs for different music releases
WIKIPEDIA_SOURCES = {
    "2026_albums": {
        "url": "https://en.wikipedia.org/wiki/List_of_2026_albums",
        "name": "General 2026 Albums",
    },
    "2026_heavy_metal": {
        "url": "https://en.wikipedia.org/wiki/2026_in_heavy_metal_music",
        "name": "Heavy Metal 2026",
    },
    "2026_rock": {
        "url": "https://en.wikipedia.org/wiki/2026_in_rock_music",
        "name": "Rock Music 2026",
    },
    "2026_kpop": {
        "url": "https://en.wikipedia.org/wiki/2026_in_South_Korean_music",
        "name": "K-Pop/Korean Music 2026",
    },
    "2026_american": {
        "url": "https://en.wikipedia.org/wiki/2026_in_American_music",
        "name": "American Music 2026",
    },
}

class WikipediaReleaseScraper:
    """Scrapes album releases from Wikipedia"""
    
    def __init__(self, db_path: str = "database.db"):
        self.db_path = db_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def get_db(self):
        """Get database connection"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def scrape_all_sources(self) -> Dict[str, any]:
        """Scrape all configured Wikipedia sources"""
        results = {
            "total_items": 0,
            "total_added": 0,
            "total_updated": 0,
            "sources": {}
        }
        
        for source_key, source_info in WIKIPEDIA_SOURCES.items():
            logger.info(f"Scraping {source_info['name']}...")
            items = self.scrape_source(source_info["url"], source_info["name"])
            
            results["sources"][source_key] = {
                "name": source_info["name"],
                "url": source_info["url"],
                "items_found": len(items),
                "items_added": 0,
                "items_updated": 0,
            }
            
            # Save items to database
            added, updated = self.save_releases(items, source_info["name"])
            results["total_items"] += len(items)
            results["total_added"] += added
            results["total_updated"] += updated
            results["sources"][source_key]["items_added"] = added
            results["sources"][source_key]["items_updated"] = updated
            
            logger.info(f"  Found {len(items)} releases ({added} new, {updated} updated)")
        
        return results
    
    def scrape_source(self, url: str, source_name: str) -> List[Dict]:
        """Scrape a single Wikipedia source"""
        try:
            logger.debug(f"Fetching {url}")
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            releases = self._parse_release_tables(soup, source_name)
            
            logger.info(f"✓ Scraped {len(releases)} releases from {source_name}")
            return releases
        except Exception as e:
            logger.error(f"Error scraping {source_name}: {e}")
            return []
    
    def _parse_release_tables(self, soup: BeautifulSoup, source_name: str) -> List[Dict]:
        """Parse release information from Wikipedia tables"""
        releases = []
        
        # Find all tables on the page
        tables = soup.find_all('table', {'class': 'wikitable'})
        logger.debug(f"Found {len(tables)} tables on {source_name}")
        
        for table in tables:
            rows = table.find_all('tr')[1:]  # Skip header row
            
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 2:
                    continue
                
                # Try to extract artist and album information
                release = self._parse_row(cells, source_name)
                if release:
                    releases.append(release)
        
        return releases
    
    def _parse_row(self, cells, source_name: str) -> Optional[Dict]:
        """Parse a single table row"""
        try:
            # Extract text from cells
            cell_text = [cell.get_text(strip=True) for cell in cells]
            
            # Try different parsing strategies based on number of columns
            artist = None
            album = None
            release_date = None
            
            if len(cell_text) >= 2:
                # Most common format: Artist | Album | Date
                artist = cell_text[0]
                album = cell_text[1]
                if len(cell_text) >= 3:
                    release_date = cell_text[2]
            
            # Clean up and validate
            artist = artist.strip() if artist else None
            album = album.strip() if album else None
            release_date = release_date.strip() if release_date else None
            
            if not artist or not album:
                return None
            
            # Try to parse the date
            year = self._extract_year(release_date or "2026")
            
            # Skip if artist or album is generic placeholder text
            if any(x in artist.lower() for x in ['edit', 'cite', 'ref']):
                return None
            if any(x in album.lower() for x in ['edit', 'cite', 'ref']):
                return None
            
            return {
                "artist_name": artist,
                "album_name": album,
                "release_date": release_date,
                "release_year": year,
                "source": source_name,
            }
        except Exception as e:
            logger.debug(f"Error parsing row: {e}")
            return None
    
    def _extract_year(self, date_str: str) -> int:
        """Extract year from date string"""
        match = re.search(r'202[6-9]|202[0-9]', date_str)
        if match:
            return int(match.group())
        return 2026
    
    def save_releases(self, releases: List[Dict], source_name: str) -> tuple:
        """Save releases to database, avoiding duplicates"""
        conn = self.get_db()
        cursor = conn.cursor()
        added = 0
        updated = 0
        
        # Get list of artists in collection (gracefully handle if tracks table doesn't exist)
        artists_in_collection = set()
        albums_in_collection = set()
        
        try:
            cursor.execute("SELECT DISTINCT LOWER(artist) FROM tracks WHERE artist IS NOT NULL")
            rows = cursor.fetchall() or []
            artists_in_collection = {row[0] for row in rows if row and row[0]}
            
            # Get list of albums in collection
            cursor.execute("SELECT DISTINCT LOWER(artist), LOWER(album) FROM tracks WHERE artist IS NOT NULL AND album IS NOT NULL")
            rows = cursor.fetchall() or []
            albums_in_collection = {(row[0], row[1]) for row in rows if row and row[0] and row[1]}
        except (sqlite3.OperationalError, TypeError) as e:
            # tracks table may not exist yet, continue without filtering
            logger.debug(f"Could not query tracks table (may not exist yet): {e}")
            artists_in_collection = set()
            albums_in_collection = set()
        
        for release in releases:
            if not release or not isinstance(release, dict):
                logger.warning(f"Skipping invalid release: {release}")
                continue
                
            artist_in_collection = release.get("artist_name", "").lower() in artists_in_collection if release.get("artist_name") else False
            album_in_collection = (release.get("artist_name", "").lower(), release.get("album_name", "").lower()) in albums_in_collection if release.get("artist_name") and release.get("album_name") else False
            
            try:
                cursor.execute("""
                    INSERT INTO upcoming_releases 
                    (artist_name, album_name, release_date, release_year, source, 
                     artist_in_collection, album_in_collection)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(artist_name, album_name, release_date) DO UPDATE SET
                    updated_at = CURRENT_TIMESTAMP,
                    artist_in_collection = excluded.artist_in_collection,
                    album_in_collection = excluded.album_in_collection
                """, (
                    release.get("artist_name", "Unknown"),
                    release.get("album_name", "Unknown"),
                    release.get("release_date"),
                    release.get("release_year", 2026),
                    source_name,
                    artist_in_collection,
                    album_in_collection,
                ))
                added += 1
            except sqlite3.IntegrityError:
                # Update existing
                cursor.execute("""
                    UPDATE upcoming_releases
                    SET artist_in_collection = ?, album_in_collection = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE artist_name = ? AND album_name = ? AND release_date = ?
                """, (artist_in_collection, album_in_collection, 
                      release.get("artist_name", ""), release.get("album_name", ""), release.get("release_date")))
                updated += 1
        
        # Log scrape
        try:
            cursor.execute("""
                INSERT INTO release_scrape_history 
                (source_url, source_name, items_found, items_added, items_updated, 
                 scrape_status, scrape_start, scrape_end)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                WIKIPEDIA_SOURCES.get(source_name, {}).get("url", ""),
                source_name,
                len(releases),
                added,
                updated,
                "success",
                datetime.now(),
                datetime.now(),
            ))
        except Exception as e:
            logger.error(f"Error logging scrape history: {e}")
        
        conn.commit()
        conn.close()
        
        return added, updated
    
    def get_upcoming_releases(self, artist_in_collection: bool = False) -> List[Dict]:
        """Get upcoming releases, optionally filtered by collection artists"""
        try:
            conn = self.get_db()
            cursor = conn.cursor()
            
            if artist_in_collection:
                query = "SELECT * FROM upcoming_releases WHERE artist_in_collection = TRUE ORDER BY release_date ASC"
            else:
                query = "SELECT * FROM upcoming_releases ORDER BY release_date ASC"
            
            cursor.execute(query)
            rows = cursor.fetchall() or []
            releases = []
            
            for row in rows:
                if row is None:
                    logger.warning("Skipping None row from database")
                    continue
                try:
                    releases.append(dict(row))
                except (TypeError, ValueError) as e:
                    logger.warning(f"Could not convert row to dict: {e}, row: {row}")
                    continue
            
            conn.close()
            return releases
        except Exception as e:
            logger.error(f"Error retrieving upcoming releases: {e}")
            return []


if __name__ == "__main__":
    scraper = WikipediaReleaseScraper()
    results = scraper.scrape_all_sources()
    
    print(f"\n✓ Scraping complete!")
    print(f"  Total items: {results['total_items']}")
    print(f"  Added: {results['total_added']}")
    print(f"  Updated: {results['total_updated']}")
