#!/usr/bin/env python3
"""
Wikipedia Album Release Scraper

Scrapes upcoming album releases from Wikipedia pages for various genres and regions.
Parses release tables and stores information in the database.
"""
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import re
import urllib.request

# Try to import requests, fall back to urllib if not available
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

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
    # Note: Genre columns will be automatically skipped during parsing
    SOURCE_COLUMN_ORDERS = {
        "2026_albums": ['day', 'artist', 'album', 'genre'],      # Day, Artist, Album, Genre (matches Wikipedia table order)
        "2026_heavy_metal": ['day', 'artist', 'album'],          # Day, Artist, Album
        "2026_rock": ['day', 'artist', 'album'],                 # Day, Artist, Album
        "2026_kpop": ['day', 'album', 'artist'],                 # Day, Album, Artist
        "2026_american": ['day', 'album', 'artist'],             # Day, Album, Artist
    }
    
    def __init__(self, db_path: str = "database.db"):
        self.db_path = db_path
        self.use_requests = HAS_REQUESTS
        
        if self.use_requests:
            # Use requests library if available
            self.session = requests.Session()  # type: ignore
            self.session.headers.update({  # type: ignore
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            # Disable SSL verification for Wikipedia (trusted source)
            self.session.verify = False  # type: ignore
        else:
            # Create urllib wrapper
            class UrllibSession:
                def __init__(self):
                    self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                
                def get(self, url: str, timeout: int = 10):
                    class Response:
                        def __init__(self, content):
                            self.content = content
                        def raise_for_status(self):
                            pass
                    request = urllib.request.Request(url, headers=self.headers)
                    # For urllib, we need to create a context that ignores SSL verification
                    import ssl
                    context = ssl.create_default_context()
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                        return Response(response.read())
            
            self.session = UrllibSession()
    
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
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            releases = self._parse_release_tables(soup, source_key, source_name)
            
            logger.info(f"[OK] Scraped {len(releases)} releases from {source_name}")
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
        
        # Extract year from source_key (e.g., "2026_albums" -> 2026)
        year = self._extract_year_from_source_key(source_key)
        logger.debug(f"Extracted year {year} from source_key '{source_key}'")
        
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
            
            # Track last seen day for handling rowspan (multiple rows with same day)
            last_seen_day = None
            
            # Parse data rows
            for row in rows[start_idx:]:
                cells = row.find_all('td')
                if len(cells) < 2:
                    continue
                
                # Debug first few rows to understand structure
                if len(releases) < 3:
                    cell_preview = [c.get_text(strip=True)[:30] for c in cells[:4]]
                    logger.info(f"Row {len(releases)} preview: {cell_preview}")
                
                release = self._parse_row_for_month(cells, source_key, source_name, current_month, year, column_order, last_seen_day)
                if release:
                    releases.append(release)
                    # Update last_seen_day if this release has a day number
                    release_date = release.get('release_date', '')
                    if release_date:
                        try:
                            day_from_date = int(release_date.split('-')[2])
                            last_seen_day = day_from_date
                        except (ValueError, IndexError):
                            pass
                    if len(releases) <= 3:
                        logger.info(f"Parsed: {release['artist_name']} - {release['album_name']} ({release['release_date']})")
        
        return releases
    
    def _is_genre_column(self, cell_value: str) -> bool:
        """Detect if a cell contains genre/style information
        
        Genre cells typically:
        - Contain multiple items separated by commas
        - Have words like "metal", "rock", "pop", "hip-hop", etc.
        - Are comma-separated
        - Often contain dashes (e.g., "Heavy-metal")
        """
        if not cell_value or len(cell_value) < 3:
            return False
        
        # Check if cell has multiple comma-separated items (typical for genres)
        if ',' in cell_value:
            parts = [p.strip() for p in cell_value.split(',')]
            # If we have 2+ parts, check if they look like genres
            if len(parts) >= 2:
                genre_keywords = ['metal', 'rock', 'pop', 'hip-hop', 'hip hop', 'punk', 'jazz', 
                                'blues', 'country', 'folk', 'electronic', 'dance', 'soul', 'funk',
                                'alternative', 'indie', 'ambient', 'experimental', 'classical']
                for part in parts[:2]:  # Check first 2 parts
                    if any(keyword in part.lower() for keyword in genre_keywords):
                        logger.debug(f"  Detected genre column: {cell_value[:40]}")
                        return True
        
        return False

    def _parse_row_for_month(self, cells, source_key: str, source_name: str, current_month: int, 
                             year: int, column_order: list, last_seen_day: Optional[int] = None) -> Optional[Dict]:
        """Parse a row using source-specific column order
        
        column_order: list like ['day', 'artist', 'genre', 'album']
        Handles rows with or without date columns (Wikipedia omits dates for same-day follow-ups).
        last_seen_day: Used when a row doesn't have a date cell (due to HTML rowspan)
        """
        try:
            if len(cells) < 2:
                return None
            
            cell_texts = [cell.get_text(strip=True) for cell in cells]
            
            # Remove citation brackets like [23], [1], etc. from all cell text
            cell_texts = [re.sub(r'\s*\[\d+\]\s*', ' ', text).strip() for text in cell_texts]
            
            logger.debug(f"Parsing row for {source_name}: {cell_texts[:5]}")  # Log first 5 cells
            
            # DETECT if first cell is a date or not
            # If row has one fewer cell than expected OR first cell is not a date, shift the mapping
            first_cell = cell_texts[0] if cell_texts else ""
            has_date_in_first_cell = bool(re.match(r'^\d{1,2}$', first_cell))  # Just a number like "9" or "16"
            
            actual_column_order = column_order.copy()
            
            # If the first cell doesn't look like a date but column_order expects one, shift left
            if 'day' in actual_column_order and not has_date_in_first_cell:
                # Remove 'day' from the expected columns since this row doesn't have one
                day_idx = actual_column_order.index('day')
                actual_column_order = actual_column_order[:day_idx] + actual_column_order[day_idx+1:]
                logger.debug(f"  Date cell missing, adjusted column order: {actual_column_order}")
            
            # Build a mapping of column types to values, skipping 'genre' columns
            col_values = {}
            cell_idx = 0
            
            for col_idx, col_type in enumerate(actual_column_order):
                if cell_idx >= len(cell_texts):
                    break
                
                cell_value = cell_texts[cell_idx].strip()
                
                # Skip empty cells
                if not cell_value:
                    cell_idx += 1
                    continue
                
                logger.debug(f"  Column {cell_idx}: col_type='{col_type}', value='{cell_value[:50]}'")
                
                # Skip genre columns - they're in the column order but we don't extract them
                if col_type == 'genre':
                    logger.debug(f"    Skipping genre cell: '{cell_value[:50]}'")
                    cell_idx += 1
                    continue
                
                # Store the value
                col_values[col_type] = cell_value
                cell_idx += 1
            
            # Extract and process the values we care about
            day = None
            artist = col_values.get('artist')
            album = col_values.get('album')
            
            # Process day - if not in col_values, it's missing and defaults to 1
            day_str = col_values.get('day')
            if day_str:
                try:
                    match = re.match(r'(\d+)', day_str)
                    if match:
                        day = int(match.group(1))
                        if not (1 <= day <= 31):
                            logger.debug(f"    Day {day} out of range, ignoring")
                            day = None
                        else:
                            logger.debug(f"    Extracted day: {day}")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"    Could not extract day from '{day_str}': {e}")
            
            # Default day to last_seen_day if not found, otherwise default to 1
            if not day:
                if last_seen_day:
                    day = last_seen_day
                    logger.debug(f"  No day column in row, using last_seen_day: {day}")
                else:
                    day = 1
                    if not day_str:
                        logger.debug(f"  No day column in row, defaulting to 1")
                    else:
                        logger.debug(f"  Could not parse day, defaulting to 1")
            
            # Validate that the day is valid for the given month/year combination
            # This prevents invalid dates like February 30 or April 31
            try:
                datetime(year, current_month, day)
            except ValueError as e:
                logger.debug(f"  Invalid date: {year}-{current_month:02d}-{day:02d} - {e}")
                logger.debug(f"  Defaulting to first day of month")
                day = 1
            
            # Validate we have artist and album
            if not artist or not album or len(artist) < 2 or len(album) < 2:
                logger.debug(f"  Invalid: artist='{artist}', album='{album}'")
                return None
            
            # Skip entries with wiki markup
            for text in [artist, album]:
                if any(x in text.lower() for x in ['cite', 'ref', 'edit', '</td>', '[citation']):
                    logger.debug(f"  Skipping due to wiki markup")
                    return None
            
            # Skip if artist or album looks like it's full of genre info
            if self._is_genre_column(artist) or self._is_genre_column(album):
                logger.debug(f"  Skipping: one field looks like genre info")
                return None
            
            # Build date
            release_date = f"{year}-{current_month:02d}-{day:02d}"
            
            logger.debug(f"  Final: {artist} - {album} ({release_date})")
            
            return {
                "artist_name": artist,
                "album_name": album,
                "release_date": release_date,
                "release_year": year,
                "source": source_name,
            }
        except Exception as e:
            logger.debug(f"Error parsing row for {source_name}: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None
    
    
    def _extract_year_from_source_key(self, source_key: str) -> int:
        """Extract year from source key (e.g., '2026_albums' -> 2026)"""
        # Match years from 2020-2099 (202x, 203x, etc.)
        match = re.search(r'(20[2-9]\d)', source_key)
        if match:
            return int(match.group(1))
        # Default to current year if no year found
        return datetime.now().year
    
    def _extract_year(self, date_str: str) -> int:
        """Extract year from date string"""
        # Match years from 2020-2029
        match = re.search(r'202\d', date_str)
        if match:
            return int(match.group())
        # Default to current year if no year found
        return datetime.now().year
    
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
                # If no year was in format, use current year
                if '%Y' not in fmt:
                    parsed = parsed.replace(year=datetime.now().year)
                return parsed.strftime('%Y-%m-%d')
            except ValueError:
                continue
        
        # Could not parse
        return None
    
    def save_releases(self, releases: List[Dict], source_name: str) -> tuple:
        """Save releases to database, avoiding duplicates
        
        Only imports releases from artists in the user's collection.
        """
        conn = self.get_db()
        cursor = conn.cursor()
        added = 0
        updated = 0
        filtered_out = 0
        
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
        
        logger.info(f"Filtering {len(releases)} releases against {len(artists_in_collection)} artists in collection")
        
        for release in releases:
            if not release or not isinstance(release, dict):
                logger.warning(f"Skipping invalid release: {release}")
                continue
            
            # Only import releases from artists in collection
            artist_name = release.get("artist_name", "")
            artist_in_collection = artist_name.lower() in artists_in_collection if artist_name else False
            
            # Skip releases from artists not in collection
            if not artist_in_collection:
                logger.debug(f"Filtered out: '{artist_name}' - {release.get('album_name')} (artist not in collection)")
                filtered_out += 1
                continue
            
            album_in_collection = (artist_name.lower(), release.get("album_name", "").lower()) in albums_in_collection if release.get("album_name") else False
            
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
                    artist_name,
                    release.get("album_name", "Unknown"),
                    release.get("release_date"),
                    release.get("release_year", datetime.now().year),
                    source_name,
                    artist_in_collection,
                    album_in_collection,
                ))
                added += 1
            except sqlite3.IntegrityError as e:
                # Update existing record
                try:
                    cursor.execute("""
                        UPDATE upcoming_releases
                        SET artist_in_collection = ?, album_in_collection = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE artist_name = ? AND album_name = ? AND release_date = ?
                    """, (artist_in_collection, album_in_collection, artist_name, release.get("album_name", ""), release.get("release_date")))
                    updated += 1
                except Exception as update_error:
                    logger.warning(f"Failed to update release - {artist_name} / {release.get('album_name')}: {update_error}")
        
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
        
        logger.info(f"{source_name}: {added} added, {updated} updated, {filtered_out} filtered out (from {len(releases)} total)")
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
            count_row = cursor.fetchone()
            count_before = count_row[0] if count_row else 0
            
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
    
    print(f"\n[OK] Scraping complete!")
    print(f"  Total items: {results['total_items']}")
    print(f"  Added: {results['total_added']}")
    print(f"  Updated: {results['total_updated']}")
