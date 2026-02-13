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
            rows = table.find_all('tr')
            current_month = None
            
            for row in rows:
                # Check if this is a month header (th elements with month names)
                headers = row.find_all('th')
                if headers:
                    header_text = headers[0].get_text(strip=True).lower()
                    # Check if header contains a month name
                    months = ['january', 'february', 'march', 'april', 'may', 'june',
                              'july', 'august', 'september', 'october', 'november', 'december']
                    for month_num, month_name in enumerate(months, 1):
                        if month_name in header_text:
                            current_month = month_num
                            break
                
                # Parse data rows
                cells = row.find_all('td')
                if len(cells) >= 2:
                    release = self._parse_row(cells, source_name, current_month)
                    if release:
                        releases.append(release)
        
        return releases
    
    def _parse_row(self, cells, source_name: str, current_month: Optional[int] = None) -> Optional[Dict]:
        """Parse a single table row"""
        try:
            # Extract text from cells
            cell_text = [cell.get_text(strip=True) for cell in cells]
            
            # Try different parsing strategies based on number of columns
            day = None
            artist = None
            album = None
            release_date = None
            
            if len(cell_text) >= 3:
                # Format: Day | Artist | Album (| Date)
                try:
                    day_str = cell_text[0].strip()
                    day = int(re.search(r'\d+', day_str).group()) if re.search(r'\d+', day_str) else None
                except (ValueError, AttributeError):
                    day = None
                
                artist = cell_text[1]
                album = cell_text[2]
                if len(cell_text) >= 4:
                    release_date = cell_text[3]
            elif len(cell_text) >= 2:
                # Format: Artist | Album | Date
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
            
            # Build proper date from day and current month if available
            if day and current_month:
                release_date = f"2026-{current_month:02d}-{day:02d}"
            else:
                # Try to parse the date string if provided
                if release_date:
                    parsed_date = self._parse_date_string(release_date)
                    if parsed_date:
                        release_date = parsed_date
                else:
                    release_date = "2026-01-01"
            
            # Skip if artist or album is generic placeholder text
            if any(x in artist.lower() for x in ['edit', 'cite', 'ref', 'citation']):
                return None
            if any(x in album.lower() for x in ['edit', 'cite', 'ref', 'citation']):
                return None
            
            # Extract year from release_date
            year = self._extract_year(release_date or "2026")
            
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
    
    def _parse_date_string(self, date_str: str) -> Optional[str]:
        """Parse various date formats and return YYYY-MM-DD"""
        if not date_str or date_str.lower() in ['unknown', 'tba', 'tbr', 'pending']:
            return None
        
        date_str = date_str.strip()
        
        # Try common date formats
        formats = [
            '%Y-%m-%d',  # 2026-01-15
            '%B %d, %Y',  # January 15, 2026
            '%b %d, %Y',  # Jan 15, 2026
            '%m/%d/%Y',  # 01/15/2026
            '%d/%m/%Y',  # 15/01/2026
            '%B %d',  # January 15 (assume 2026)
            '%b %d',  # Jan 15 (assume 2026)
        ]
        
        for fmt in formats:
            try:
                parsed = datetime.strptime(date_str, fmt)
                # If no year was in format, use 2026
                if '%Y' not in fmt:
                    parsed = parsed.replace(year=2026)
                return parsed.strftime('%Y-%m-%d')
            except ValueError:
                continue
        
        # Try extracting day and month with regex: "January 15" or "15 January"
        day_month_match = re.search(r'(\d{1,2})\s+(\w+)', date_str)
        month_day_match = re.search(r'(\w+)\s+(\d{1,2})', date_str)
        
        if day_month_match:
            try:
                day = int(day_month_match.group(1))
                month_str = day_month_match.group(2)
                date_with_year = f"{month_str} {day}, 2026"
                parsed = datetime.strptime(date_with_year, '%B %d, %Y')
                return parsed.strftime('%Y-%m-%d')
            except (ValueError, AttributeError):
                pass
        
        if month_day_match:
            try:
                month_str = month_day_match.group(1)
                day = int(month_day_match.group(2))
                date_with_year = f"{month_str} {day}, 2026"
                parsed = datetime.strptime(date_with_year, '%B %d, %Y')
                return parsed.strftime('%Y-%m-%d')
            except (ValueError, AttributeError):
                pass
        
        return None
    
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
                    release_dict = dict(row)
                    # Ensure release_date is never None (default to 'Unknown')
                    if not release_dict.get('release_date'):
                        release_dict['release_date'] = 'Unknown'
                    releases.append(release_dict)
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
