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
import os

# Suppress SSL warnings from urllib3 (Wikipedia requests work fine without verification)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
        "2026_albums": ['day', 'artist', 'album', 'genre'],      # Day, Artist, Album, Genre (Wikipedia general albums page structure)
        "2026_heavy_metal": ['day', 'artist', 'album'],          # Day, Artist, Album
        "2026_rock": ['day', 'artist', 'album'],                 # Day, Artist, Album
        "2026_kpop": ['day', 'album', 'artist'],                 # Day, Album, Artist
        "2026_american": ['day', 'album', 'artist'],             # Day, Album, Artist
    }
    
    def __init__(self, db_path: str = None):
        # Use provided db_path, environment variable, or default paths
        if db_path:
            self.db_path = db_path
        else:
            # Try environment variable first
            self.db_path = os.environ.get("DB_PATH")
            
            # If not set, try config file
            if not self.db_path:
                try:
                    import yaml
                    if os.path.exists("config.yaml"):
                        with open("config.yaml", "r") as f:
                            cfg = yaml.safe_load(f) or {}
                            self.db_path = cfg.get("database", {}).get("path")
                except Exception:
                    pass
            
            # Fall back to Docker path, then local path
            if not self.db_path:
                if os.path.exists("/database/sptnr.db"):
                    self.db_path = "/database/sptnr.db"
                else:
                    self.db_path = "database.db"
        
        # Initialize MusicBrainz lookup cache
        self._mbz_cache = {}
        
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
    
    @staticmethod
    def normalize_artist_name(artist_name: str) -> str:
        """Normalize artist name by removing extra spaces, punctuation, and accents.
        
        Examples:
        - "Pale Face Swiss" -> "pale face swiss"
        - "Paleface Swiss" -> "paleface swiss"
        - "The Beatles" -> "the beatles"
        - "A.F.I." -> "afi"
        - "Mötley Crüe" -> "motley crue"
        - "Ben Folds Five" -> "ben folds five"
        """
        if not artist_name:
            return ""
        
        # Remove accents and diacritics
        try:
            import unicodedata
            normalized = ''.join(
                c for c in unicodedata.normalize('NFD', artist_name)
                if unicodedata.category(c) != 'Mn'
            )
        except Exception:
            normalized = artist_name
        
        # Remove common punctuation/symbols (dots, dashes within artist names)
        # But keep alphanumeric and spaces
        normalized = re.sub(r'[^\w\s]', '', normalized, flags=re.UNICODE)
        
        # Normalize whitespace (multiple spaces -> single space)
        normalized = re.sub(r'\s+', ' ', normalized)
        
        # Convert to lowercase and strip
        normalized = normalized.strip().lower()
        
        return normalized
    
    @staticmethod
    def normalize_album_name(album_name: str) -> str:
        """Normalize album name by removing common suffixes and annotations.
        
        Examples:
        - "The Wilted EP" -> "the wilted"
        - "The Wilted EP(EP)" -> "the wilted"
        - "Album Name (Deluxe Edition)" -> "album name"
        - "Album Name [Remaster]" -> "album name"
        - "Album Name (feat. Artist)" -> "album name"
        """
        if not album_name:
            return ""
        
        # Remove common suffixes in parentheses or brackets
        # Remove (EP), (LP), (Album), (Album EP), (Deluxe), (Deluxe Edition), etc.
        normalized = re.sub(r'\s*[\[\(](EP|LP|Album|Deluxe|Deluxe Edition|Remaster|Remastered|Extended|Single|Feat|feat|Featuring|feat\.|Bonus|Expanded|Anniversary|Edition|Mix|Unofficial|Limited|Special)[\]\)].*$', '', album_name, flags=re.IGNORECASE)
        
        # Also remove trailing (something) or [something] that remains
        normalized = re.sub(r'\s*[\[\(].*[\]\)]$', '', normalized)
        
        # Also remove trailing format suffixes that are NOT in parentheses/brackets
        # e.g. "Album Name EP" -> "Album Name", "Track Title - Single" -> "Track Title"
        # This catches duplicates like "The Wilted EP" vs "The Wilted EP(EP)"
        normalized = re.sub(r'\s+(EP|LP|Album|Deluxe Edition|Deluxe|Remaster|Remastered|Extended|Single|EP\s*EP)$', '', normalized, flags=re.IGNORECASE)
        
        # Strip extra whitespace and convert to lowercase
        normalized = normalized.strip().lower()
        
        return normalized
    
    def find_existing_normalized_album(self, artist_name: str, album_name: str, release_date: str) -> Optional[Dict]:
        """Find an existing album with normalized artist and album names.
        
        Uses both artist and album name normalization to catch duplicates where:
        - "Pale Face Swiss" vs "Paleface Swiss"
        - "Album (EP)" vs "Album EP(EP)"
        
        Returns the existing record if found, None otherwise.
        """
        try:
            normalized_album = self.normalize_album_name(album_name)
            normalized_artist = self.normalize_artist_name(artist_name)
            
            if not normalized_album or not normalized_artist:
                return None
            
            conn = self.get_db()
            cursor = conn.cursor()
            
            # Query all albums on same date and check normalized names
            cursor.execute("""
                SELECT * FROM upcoming_releases 
                WHERE release_date = ?
            """, (release_date,))
            
            rows = cursor.fetchall() or []
            conn.close()
            
            for row in rows:
                if row is None:
                    continue
                    
                existing_artist = row[1] if isinstance(row, tuple) else row.get('artist_name')
                existing_album = row[2] if isinstance(row, tuple) else row.get('album_name')
                
                # Compare normalized versions
                if (self.normalize_artist_name(existing_artist) == normalized_artist and
                    self.normalize_album_name(existing_album) == normalized_album):
                    return dict(row) if not isinstance(row, dict) else row
            
            return None
        except Exception as e:
            logger.warning(f"Error finding normalized album: {e}")
            return None
    
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
        """Parse release information from Wikipedia tables with proper rowspan/colspan handling
        
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
            initial_day = None
            
            # Walk backwards from table through all previous elements looking for month
            prev = table.find_previous()
            search_depth = 0
            max_search = 50  # Limit search depth
            while prev and not current_month and search_depth < max_search:
                text = prev.get_text(strip=True).lower()
                text_original = prev.get_text(strip=True)
                
                # Check if this element contains a month name
                for month_name, month_num in months.items():
                    if month_name in text and len(text) < 100:  # Month heading is usually short
                        current_month = month_num
                        logger.info(f"✓ Found month heading '{text_original}' -> month {month_num}")
                        
                        # Extract day number from heading (e.g., "January 9" -> 9)
                        day_match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', text_original)
                        if day_match:
                            try:
                                initial_day = int(day_match.group(1))
                                if not (1 <= initial_day <= 31):
                                    initial_day = None
                                else:
                                    logger.debug(f"  Extracted initial day from heading: {initial_day}")
                            except (ValueError, AttributeError):
                                pass
                        break
                
                # Don't search too far back (stop at next table or major heading)
                if prev.name in ['table']:
                    break
                prev = prev.find_previous()
            
            if not current_month:
                current_month = 1  # Default to January
                logger.debug(f"Could not find month heading, defaulting to January")
            
            # Parse table with rowspan/colspan handling
            rows = table.find_all('tr')
            if not rows:
                continue
            
            # Skip header rows BEFORE reconstruction (TH/TD distinction is lost after)
            start_idx = 0
            for check_row_idx in range(min(5, len(rows))):
                cells_in_row = rows[check_row_idx].find_all(['td', 'th'])
                if not cells_in_row:
                    continue
                    
                th_count = sum(1 for cell in cells_in_row if cell.name == 'th')
                td_count = sum(1 for cell in cells_in_row if cell.name == 'td')
                
                # If mostly TH elements, it's a header row
                if th_count >= 3 and th_count > td_count:
                    start_idx = check_row_idx + 1
                    logger.info(f"  Skipping header row {check_row_idx} ({th_count} TH, {td_count} TD)")
                    break
            
            # Reconstruct table accounting for rowspan/colspan, skipping header rows
            reconstructed_rows = self._reconstruct_table_rows(rows[start_idx:])
            
            logger.debug(f"Starting data row parsing from row index {start_idx}, {len(reconstructed_rows)} data rows")
            
            # Track last seen day for handling rowspan (multiple rows with same day)
            last_seen_day = initial_day if initial_day else None
            
            # Parse data rows
            row_num = 0
            for cells_list in reconstructed_rows:
                if len(cells_list) < 2:
                    row_num += 1
                    continue
                if len(cells_list) < 2:
                    row_num += 1
                    continue
                
                # Debug: show ALL rows to diagnose column alignment
                cell_preview = [c[:40] if isinstance(c, str) else str(c)[:40] for c in cells_list[:6]]
                logger.info(f"[ROW {row_num}] cells={len(cells_list)} first 6: {cell_preview}")
                
                release, had_date_cell = self._parse_row_for_month_from_strings(
                    cells_list, source_key, source_name, current_month, year, column_order, last_seen_day
                )
                if release:
                    logger.info(f"[ROW {row_num}] SUCCESS: {release['artist_name']} - {release['album_name']} ({release['release_date']}) had_date={had_date_cell}")
                    releases.append(release)
                    # Update last_seen_day ONLY if this row had an actual date cell (not a default)
                    if had_date_cell:
                        release_date = release.get('release_date', '')
                        if release_date:
                            try:
                                day_from_date = int(release_date.split('-')[2])
                                last_seen_day = day_from_date
                                logger.debug(f"    Updated last_seen_day to {day_from_date}")
                            except (ValueError, IndexError):
                                pass
                else:
                    logger.info(f"[ROW {row_num}] SKIPPED")
                
                row_num += 1
        
        return releases
    
    def _reconstruct_table_rows(self, rows) -> List[List[str]]:
        """Reconstruct table rows accounting for rowspan and colspan
        
        Returns a list of lists where each inner list contains cell values as strings.
        Cells affected by rowspan from previous rows are carried forward.
        """
        reconstructed = []
        col_tracking = {}  # {col_idx: (value, rows_remaining)}
        
        for row_idx, row in enumerate(rows):
            cells = row.find_all(['td', 'th'])
            row_cells = []
            col_idx = 0
            
            for cell in cells:
                # Skip columns that are still covered by rowspan from previous rows
                while col_idx in col_tracking and col_tracking[col_idx][1] > 0:
                    row_cells.append(col_tracking[col_idx][0])
                    col_tracking[col_idx] = (col_tracking[col_idx][0], col_tracking[col_idx][1] - 1)
                    col_idx += 1
                
                # Get cell value
                cell_value = cell.get_text(strip=True)
                row_cells.append(cell_value)
                
                # Handle rowspan and colspan
                rowspan = int(cell.get('rowspan', 1))
                colspan = int(cell.get('colspan', 1))
                
                # Track this cell for future rows (rowspan > 1)
                if rowspan > 1:
                    col_tracking[col_idx] = (cell_value, rowspan - 1)
                
                # Move col_idx forward by colspan
                col_idx += colspan
            
            # Add any remaining tracked cells for this row
            while col_idx in col_tracking and col_tracking[col_idx][1] > 0:
                row_cells.append(col_tracking[col_idx][0])
                col_tracking[col_idx] = (col_tracking[col_idx][0], col_tracking[col_idx][1] - 1)
                col_idx += 1
            
            if row_cells:
                reconstructed.append(row_cells)
        
        return reconstructed
    
    def _parse_row_for_month_from_strings(self, cell_texts: list, source_key: str, source_name: str, current_month: int, 
                                          year: int, column_order: list, last_seen_day: Optional[int] = None) -> tuple:
        """Parse a row using string values instead of cell objects
        
        Returns: (release_dict, had_date_cell) where had_date_cell indicates if actual date cell found
        """
        try:
            if len(cell_texts) < 2:
                return None, False
            
            # Remove citation brackets like [23], [1], etc. from all cell text
            cell_texts = [re.sub(r'\s*\[\d+\]\s*', ' ', text).strip() for text in cell_texts]
            
            logger.debug(f"Parsing row for {source_name}: {len(cell_texts)} cells, {column_order} column order")
            logger.debug(f"  Raw cells: {[f'{i}={repr(c[:50])}' for i, c in enumerate(cell_texts[:6])]}")
            logger.debug(f"  Raw cells (first 3): [{repr(cell_texts[0][:40])}, {repr(cell_texts[1][:40] if len(cell_texts) > 1 else 'N/A')}, {repr(cell_texts[2][:40] if len(cell_texts) > 2 else 'N/A')}]")
            
            # DETECT if first cell is a date or not
            first_cell = cell_texts[0] if cell_texts else ""
            # Match day numbers with or without "January" prefix (e.g., "January1", "1st", "1")
            date_match = re.search(r'(\d{1,2})(?:st|nd|rd|th)?(?:\s|$)', first_cell)
            has_date_in_first_cell = bool(date_match)
            
            logger.debug(f"  First cell: {repr(first_cell[:40])}, has_date={has_date_in_first_cell}")
            
            actual_column_order = column_order.copy()
            had_date_cell = has_date_in_first_cell
            
            # If the first cell doesn't look like a date but column_order expects one, shift left
            if 'day' in actual_column_order and not has_date_in_first_cell:
                day_idx = actual_column_order.index('day')
                actual_column_order = actual_column_order[:day_idx] + actual_column_order[day_idx+1:]
                logger.debug(f"  Date cell missing, adjusted column order: {actual_column_order}")
            
            # Build a mapping of column types to values
            col_values = {}
            cell_idx = 0
            
            logger.debug(f"  Processing {len(actual_column_order)} columns from {len(cell_texts)} cells")
            logger.debug(f"  Column order: {actual_column_order}")
            logger.debug(f"  Cell texts: {[f'{i}={repr(c[:30])}' for i, c in enumerate(cell_texts[:6])]}")
            
            for col_idx, col_type in enumerate(actual_column_order):
                if cell_idx >= len(cell_texts):
                    logger.debug(f"  col_idx={col_idx}, col_type={col_type}: OUT OF CELLS (cell_idx={cell_idx})")
                    break
                
                cell_value = cell_texts[cell_idx].strip()
                
                logger.debug(f"  col_idx={col_idx}, col_type={col_type}, cell_idx={cell_idx}: value={repr(cell_value[:40])}")
                
                if not cell_value:
                    logger.debug(f"    -> Empty, skipping")
                    cell_idx += 1
                    continue
                
                if col_type == 'genre':
                    logger.debug(f"    -> Skipping genre")
                    cell_idx += 1
                    continue
                
                logger.debug(f"    -> Mapping {col_type} = {repr(cell_value[:50])}")
                col_values[col_type] = cell_value
                cell_idx += 1
            
            logger.debug(f"  Final col_values: {col_values}")
            
            # Extract and process values
            day = None
            artist = col_values.get('artist')
            album = col_values.get('album')
            
            day_str = col_values.get('day')
            if day_str:
                try:
                    match = re.search(r'(\d{1,2})(?:st|nd|rd|th)?(?:\s|$)', day_str)
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
            
            # Validate date
            try:
                release_dt = datetime(year, current_month, day)
            except ValueError as e:
                logger.debug(f"  Invalid date: {year}-{current_month:02d}-{day:02d} - {e}")
                logger.debug(f"  Defaulting to first day of month")
                day = 1
            
            # Skip TBA (To Be Announced) entries
            if artist and album:
                if artist.upper() == 'TBA' or album.upper() == 'TBA':
                    logger.debug(f"  Skipping TBA entry: artist='{artist}', album='{album}'")
                    return None, had_date_cell
            
            # Validate artist and album
            if not artist or not album or len(artist) < 2 or len(album) < 2:
                logger.debug(f"  Invalid: artist='{artist}', album='{album}'")
                return None, had_date_cell
            
            # Skip wiki markup
            for text in [artist, album]:
                if any(x in text.lower() for x in ['cite', 'ref', 'edit', '</td>', '[citation']):
                    logger.debug(f"  Skipping due to wiki markup")
                    return None, had_date_cell
            
            # Skip genre-like fields
            if self._is_genre_column(artist) or self._is_genre_column(album):
                logger.debug(f"  Skipping: one field looks like genre info")
                return None, had_date_cell
            
            release_date = f"{year}-{current_month:02d}-{day:02d}"
            
            # Attempt MusicBrainz lookup to validate and enrich artist/album names
            mb_result = self._lookup_musicbrainz(artist, album, release_date)
            if mb_result:
                # Use corrected names from MusicBrainz if found
                artist = mb_result.get('artist_name', artist)
                album = mb_result.get('album_name', album)
                logger.debug(f"  MusicBrainz match applied: {artist} - {album}")
            
            logger.debug(f"  Final: {artist} - {album} ({release_date})")
            
            return {
                "artist_name": artist,
                "album_name": album,
                "release_date": release_date,
                "release_year": year,
                "source": source_name,
            }, had_date_cell
        except Exception as e:
            logger.debug(f"Error parsing row for {source_name}: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None, False
    
    def _lookup_musicbrainz(self, artist: str, album: str, release_date: str) -> dict | None:
        """Look up artist and album in MusicBrainz to validate/correct names.
        
        Args:
            artist: Artist name to look up
            album: Album name to look up  
            release_date: Release date for context (YYYY-MM-DD format)
        
        Returns:
            Dict with corrected artist_name and album_name, or None if not found
        """
        # Use cache to avoid repeated lookups for the same artist/album combination
        cache_key = f"{artist.lower()}|{album.lower()}"
        if cache_key in self._mbz_cache:
            return self._mbz_cache[cache_key]
        
        try:
            from api_clients.musicbrainz import MusicBrainzClient
            from requests.exceptions import RequestException, Timeout
            
            mb = MusicBrainzClient()
            
            # Try to find the release by artist + album + date
            try:
                releases = mb.search_releases(artist=artist, album=album, date=release_date)
                if releases:
                    release = releases[0]
                    corrected_artist = release.get('artist-credit-phrase') or artist
                    corrected_album = release.get('title') or album
                    
                    if corrected_artist != artist or corrected_album != album:
                        logger.debug(f"    MusicBrainz release match: '{artist}' -> '{corrected_artist}', '{album}' -> '{corrected_album}'")
                        result = {
                            'artist_name': corrected_artist,
                            'album_name': corrected_album,
                            'mbid': release.get('id')
                        }
                        self._mbz_cache[cache_key] = result
                        return result
                    else:
                        logger.debug(f"    MusicBrainz confirmed: {artist} - {album}")
                        result = {
                            'artist_name': corrected_artist,
                            'album_name': corrected_album,
                            'mbid': release.get('id')
                        }
                        self._mbz_cache[cache_key] = result
                        return result
            except (RequestException, Timeout) as e:
                logger.debug(f"    MusicBrainz release search network error: {type(e).__name__}")
            except Exception as e:
                logger.debug(f"    MusicBrainz release search failed: {e}")
            
            # If release search didn't work, try artist lookup to validate name
            try:
                artists = mb.search_artists(artist)
                if artists:
                    corrected_artist = artists[0].get('name') or artist
                    if corrected_artist != artist:
                        logger.debug(f"    MusicBrainz artist correction: '{artist}' -> '{corrected_artist}'")
                        result = {
                            'artist_name': corrected_artist,
                            'album_name': album
                        }
                        self._mbz_cache[cache_key] = result
                        return result
            except (RequestException, Timeout) as e:
                logger.debug(f"    MusicBrainz artist search network error: {type(e).__name__}")
            except Exception as e:
                logger.debug(f"    MusicBrainz artist search failed: {e}")
        
        except ImportError:
            logger.debug("    MusicBrainz client not available")
        except Exception as e:
            logger.debug(f"    MusicBrainz lookup error: {e}")
        
        # Cache the None result to avoid repeated failed lookups
        self._mbz_cache[cache_key] = None
        return None
    
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
            
            # Mark releases not in collection, but still add them so they can be viewed
            if not artist_in_collection:
                logger.debug(f"Adding release from artist not in collection: '{artist_name}' - {release.get('album_name')}")
            
            album_in_collection = (artist_name.lower(), release.get("album_name", "").lower()) in albums_in_collection if release.get("album_name") else False
            
            album_name = release.get("album_name", "Unknown")
            release_date = release.get("release_date")
            
            # Check if a normalized version of this album already exists
            existing_normalized = self.find_existing_normalized_album(artist_name, album_name, release_date)
            
            try:
                if existing_normalized:
                    # Update the existing normalized album record with better info if available
                    existing_id = existing_normalized.get('id')
                    logger.debug(f"Found normalized duplicate: '{existing_normalized.get('album_name')}' == '{album_name}' - updating record")
                    
                    # Use the shorter/better formatted name if the current one is better
                    # (e.g., prefer "The Wilted EP" over "The Wilted EP(EP)")
                    better_name = album_name if len(album_name) < len(existing_normalized.get('album_name', '')) else existing_normalized.get('album_name')
                    
                    cursor.execute("""
                        UPDATE upcoming_releases
                        SET artist_in_collection = ?, album_in_collection = ?, updated_at = CURRENT_TIMESTAMP, album_name = ?
                        WHERE id = ?
                    """, (artist_in_collection, album_in_collection, better_name, existing_id))
                    updated += 1
                else:
                    # Insert new record
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
                        album_name,
                        release_date,
                        release.get("release_year", datetime.now().year),
                        source_name,
                        artist_in_collection,
                        album_in_collection,
                    ))
                    added += 1
            except sqlite3.IntegrityError as e:
                # Update existing record on constraint violation
                try:
                    cursor.execute("""
                        UPDATE upcoming_releases
                        SET artist_in_collection = ?, album_in_collection = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE artist_name = ? AND album_name = ? AND release_date = ?
                    """, (artist_in_collection, album_in_collection, artist_name, album_name, release_date))
                    updated += 1
                except Exception as update_error:
                    logger.warning(f"Failed to update release - {artist_name} / {album_name}: {update_error}")
        
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
    import sys
    
    # Parse command-line arguments
    db_path = None
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    
    scraper = WikipediaReleaseScraper(db_path)
    logger.info(f"Using database: {scraper.db_path}")
    
    results = scraper.scrape_all_sources()
    
    print(f"\n[OK] Scraping complete!")
    print(f"  Total items: {results['total_items']}")
    print(f"  Added: {results['total_added']}")
    print(f"  Updated: {results['total_updated']}")
