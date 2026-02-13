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
    
    # Column order for each source: [position0, position1, position2, ...]
    # Positions: 'day', 'artist', 'album'
    SOURCE_COLUMN_ORDERS = {
        "2026_albums": ['day', 'artist', 'album'],      # Number, Artist, Album
        "2026_heavy_metal": ['day', 'artist', 'album'],  # Day, Artist, Album
        "2026_rock": ['day', 'artist', 'album'],         # Number, Artist, Album
        "2026_kpop": ['day', 'album', 'artist'],         # Number, Album, Artist
        "2026_american": ['day', 'album', 'artist'],     # Number, Album, Artist
    }
    
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
            items = self.scrape_source(source_key, source_info["url"], source_info["name"])
            
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
    
    def scrape_source(self, source_key: str, url: str, source_name: str) -> List[Dict]:
        """Scrape a single Wikipedia source"""
        try:
            logger.debug(f"Fetching {url}")
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            releases = self._parse_release_tables(soup, source_key, source_name)
            
            logger.info(f"✓ Scraped {len(releases)} releases from {source_name}")
            return releases
        except Exception as e:
            logger.error(f"Error scraping {source_name}: {e}")
            return []
    
    def _parse_release_tables(self, soup: BeautifulSoup, source_key: str, source_name: str) -> List[Dict]:
        """Parse release information from Wikipedia tables
        
        Expected structure:
        - Month heading (h2/h3/text with month name)
        - Table with releases for that month
        - Columns vary by source (see SOURCE_COLUMN_ORDERS)
        """
        releases = []
        
        months = {
            'january': 1, 'february': 2, 'march': 3, 'april': 4,
            'may': 5, 'june': 6, 'july': 7, 'august': 8,
            'september': 9, 'october': 10, 'november': 11, 'december': 12
        }
        
        # Get column order for this source
        column_order = self.SOURCE_COLUMN_ORDERS.get(source_key, ['day', 'artist', 'album'])
        logger.debug(f"Using column order for {source_name}: {column_order}")
        
        # Find all tables on the page
        tables = soup.find_all('table', {'class': 'wikitable'})
        logger.debug(f"Found {len(tables)} tables on {source_name}")
        
        for table in tables:
            # Find the month heading before this table
            current_month = None
            
            # Walk backwards from table through all previous elements looking for month
            prev = table.find_previous()
            while prev and not current_month:
                text = prev.get_text(strip=True).lower()
                # Check if this element contains a month name
                for month_name, month_num in months.items():
                    if month_name in text and len(text) < 100:  # Month heading is usually short
                        current_month = month_num
                        logger.debug(f"Found month heading '{text}' -> month {month_num}")
                        break
                
                # Don't search too far back (stop at next table or major heading)
                if prev.name in ['table']:
                    break
                prev = prev.find_previous()
            
            if not current_month:
                current_month = 1  # Default to January
                logger.debug(f"Could not find month heading, defaulting to January")
            
            # Parse table rows
            rows = table.find_all('tr')
            if not rows:
                continue
            
            # Skip header row (first row usually has th elements)
            start_idx = 0
            if rows and rows[0].find_all('th'):
                start_idx = 1
                logger.debug(f"Skipping header row (contains <th> elements) for month {current_month}")
            elif rows and len(rows) > 1:
                # Check if first row looks like a header by its content
                first_row_cells = rows[0].find_all('td')
                if first_row_cells:
                    first_row_text = [c.get_text(strip=True).lower() for c in first_row_cells]
                    is_header = any(keyword in text for text in first_row_text 
                                  for keyword in ['date', 'artist', 'album', 'title', 'release', 'day', 'number'])
                    if is_header:
                        start_idx = 1
                        logger.debug(f"Skipping header row (contains column names) for month {current_month}")
            
            logger.debug(f"Starting data row parsing from index {start_idx}")
            
            # Parse data rows
            for row in rows[start_idx:]:
                cells = row.find_all('td')
                if len(cells) < 2:
                    continue
                
                # Debug first few rows to understand structure
                if len(releases) < 3:
                    cell_preview = [c.get_text(strip=True)[:30] for c in cells[:4]]
                    logger.info(f"Row {len(releases)} preview: {cell_preview}")
                
                release = self._parse_row_for_month(cells, source_key, source_name, current_month, column_order)
                if release:
                    releases.append(release)
                    if len(releases) <= 3:
                        logger.info(f"Parsed: {release['artist_name']} - {release['album_name']} ({release['release_date']})")
        
        return releases
    
    def _parse_row_for_month(self, cells, source_key: str, source_name: str, current_month: int, 
                             column_order: list) -> Optional[Dict]:
        """Parse a row using source-specific column order
        
        column_order: list like ['day', 'artist', 'album'] indicating what each column contains
        """
        try:
            if len(cells) < len([c for c in column_order if c]):  # At least the important columns
                return None
            
            cell_texts = [cell.get_text(strip=True) for cell in cells]
            
            # Remove citation brackets like [23], [1], etc. from all cell text
            cell_texts = [re.sub(r'\s*\[\d+\]\s*', ' ', text).strip() for text in cell_texts]
            
            # Debug: log what we're parsing
            logger.debug(f"Raw cells for {source_name}: {cell_texts[:4]}")  # First 4 cells
            
            # Extract fields based on column order
            artist = None
            album = None
            day = None
            
            for col_idx, col_type in enumerate(column_order):
                if col_idx >= len(cell_texts):
                    break
                
                cell_value = cell_texts[col_idx].strip()
                if not cell_value:
                    continue
                
                logger.debug(f"  Column {col_idx} ({col_type}): '{cell_value[:50]}'")
                
                if col_type == 'day':
                    # Extract number from cell (handle cases like "9", "9.", etc.)
                    try:
                        # Use regex to extract the leading number
                        match = re.match(r'(\d+)', cell_value)
                        if match:
                            day = int(match.group(1))
                            if not (1 <= day <= 31):
                                logger.debug(f"    Day {day} out of range, ignoring")
                                day = None
                            else:
                                logger.debug(f"    Extracted day: {day} from '{cell_value}'")
                        else:
                            logger.debug(f"    No number found in '{cell_value}'")
                    except (ValueError, AttributeError) as e:
                        logger.debug(f"    Could not extract day from '{cell_value}': {e}")
                elif col_type == 'artist':
                    artist = cell_value
                    logger.debug(f"    Artist: {artist[:40]}")
                elif col_type == 'album':
                    album = cell_value
                    logger.debug(f"    Album: {album[:40]}")
            
            # If we didn't get a day, default to 1
            if not day:
                day = 1
                logger.debug(f"  No day found, defaulting to 1")
            
            # Validate we have artist and album
            if not artist or not album or len(artist) < 2 or len(album) < 2:
                logger.debug(f"  Invalid: artist={artist}, album={album}")
                return None
            
            # Skip entries with wiki markup
            for text in [artist, album]:
                if any(x in text.lower() for x in ['cite', 'ref', 'edit', '</td>', '[citation']):
                    logger.debug(f"  Skipping due to wiki markup")
                    return None
            
            # Build date
            release_date = f"2026-{current_month:02d}-{day:02d}"
            
            logger.debug(f"  Final: {artist} - {album} ({release_date})")
            
            return {
                "artist_name": artist,
                "album_name": album,
                "release_date": release_date,
                "release_year": 2026,
                "source": source_name,
            }
        except Exception as e:
            logger.debug(f"Error parsing row for {source_name}: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None
    
    
    def _extract_year(self, date_str: str) -> int:
        """Extract year from date string"""
        match = re.search(r'202[6-9]|202[0-9]', date_str)
        if match:
            return int(match.group())
        return 2026
    
    def _parse_date_string(self, date_str: str) -> Optional[str]:
        """Parse various date formats and return YYYY-MM-DD"""
        if not date_str or date_str.lower() in ['unknown', 'tba', 'tbr', 'pending', '']:
            return None
        
        date_str = date_str.strip()
        
        # Try simple formats first
        formats = [
            '%Y-%m-%d',  # 2026-01-15
            '%m/%d/%Y',  # 01/15/2026
            '%d/%m/%Y',  # 15/01/2026
            '%B %d, %Y',  # January 15, 2026
            '%b %d, %Y',  # Jan 15, 2026
            '%d %B %Y',  # 15 January 2026
            '%d %b %Y',  # 15 Jan 2026
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
        
        # Could not parse
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
    
    def clear_upcoming_releases(self) -> Dict:
        """Clear all upcoming releases from the database"""
        try:
            conn = self.get_db()
            cursor = conn.cursor()
            
            # Get count before deletion
            cursor.execute("SELECT COUNT(*) FROM upcoming_releases")
            count_before = cursor.fetchone()[0] if cursor.fetchone() else 0
            
            cursor.execute("DELETE FROM upcoming_releases")
            
            conn.commit()
            conn.close()
            
            logger.info(f"Cleared {count_before} upcoming releases from database")
            
            return {
                "success": True,
                "cleared": count_before,
                "message": f"Cleared {count_before} upcoming releases"
            }
        except Exception as e:
            logger.error(f"Error clearing upcoming releases: {e}")
            return {
                "success": False,
                "error": str(e)
            }


if __name__ == "__main__":
    scraper = WikipediaReleaseScraper()
    results = scraper.scrape_all_sources()
    
    print(f"\n✓ Scraping complete!")
    print(f"  Total items: {results['total_items']}")
    print(f"  Added: {results['total_added']}")
    print(f"  Updated: {results['total_updated']}")
