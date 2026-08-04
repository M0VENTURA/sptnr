"""Wikipedia album release scraper service.

Scrapes upcoming album releases from multiple Wikipedia pages using
BeautifulSoup for robust HTML table parsing. Supports multiple genres
and regions, with collection filtering and MusicBrainz validation.

Key Functions:
    - WikipediaReleaseScraper: Main scraper class with multi-source support.
    - scrape(): Convenience wrapper for one-shot scraping.

Architecture:
    Uses ``WikipediaClient`` from ``api_clients.wikipedia`` for HTTP requests.
    Delegates persistence to ``db.repositories.upcoming_releases``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import text

from api_clients.wikipedia import WikipediaClient
from db.engine import db_session

logger = logging.getLogger(__name__)

# Default Wikipedia sources for upcoming releases.
# "List_of_upcoming_albums" was deleted — the "List of <year> albums" pages
# (month-by-month release tables) are the canonical source, matching the
# legacy scraper. The "{year}_in_music" hub pages contain NO release tables
# (they only link to the List pages), so they are used as fallbacks only.
_year = datetime.now().year
DEFAULT_WIKIPEDIA_SOURCES: dict[str, dict[str, Any]] = {
    f"{_year}_albums": {
        "url": f"https://en.wikipedia.org/wiki/List_of_{_year}_albums",
        "name": f"{_year} Albums",
        "columns": ["day", "artist", "album", "genre"],
        "fallback_titles": [f"{_year}_in_music"],
    },
    f"{_year}_heavy_metal": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_heavy_metal_music",
        "name": f"Heavy Metal {_year}",
        "columns": ["day", "artist", "album"],
        "fallback_titles": [],
    },
    f"{_year}_rock": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_rock_music",
        "name": f"Rock Music {_year}",
        "columns": ["day", "artist", "album"],
        "fallback_titles": [],
    },
    f"{_year}_kpop": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_South_Korean_music",
        "name": f"K-Pop/Korean Music {_year}",
        "columns": ["day", "album", "artist"],
        "fallback_titles": [],
    },
    f"{_year}_american": {
        "url": f"https://en.wikipedia.org/wiki/{_year}_in_American_music",
        "name": f"American Music {_year}",
        "columns": ["day", "album", "artist"],
        "fallback_titles": [],
    },
    f"{_year + 1}_albums": {
        "url": f"https://en.wikipedia.org/wiki/List_of_{_year + 1}_albums",
        "name": f"{_year + 1} Albums",
        "columns": ["day", "artist", "album", "genre"],
        "fallback_titles": [f"{_year + 1}_in_music"],
    },
}

# Month name -> number mapping
_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Genre-like cell detection keywords
_GENRE_KEYWORDS: frozenset[str] = frozenset({
    "metal", "rock", "pop", "hip-hop", "hip hop", "punk", "jazz",
    "blues", "country", "folk", "electronic", "dance", "soul", "funk",
    "alternative", "indie", "ambient", "experimental", "classical",
    "r&b", "rnb", "reggae", "latin", "world", "new age",
})


class WikipediaReleaseScraper:
    """Scrapes upcoming album releases from Wikipedia pages."""

    def __init__(self, sources: dict[str, dict[str, Any]] | None = None):
        self.sources = sources or self._load_configured_sources()
        self.http = WikipediaClient()

    def _load_configured_sources(self) -> dict[str, dict[str, Any]]:
        """Load sources from config (``upcoming_releases.sources``).

        Mirrors the legacy scraper: config entries win, defaults are the
        fallback when nothing is configured. Sources disabled in the config
        page (``enabled: false``) are skipped.
        """
        try:
            from helpers.config_helpers import get_upcoming_releases_sources
            loaded: dict[str, dict[str, Any]] = {}
            for s in get_upcoming_releases_sources() or []:
                if not isinstance(s, dict):
                    continue
                if s.get("enabled", True) is False:
                    continue
                key = str(s.get("key", "")).strip()
                url = str(s.get("url", "")).strip()
                if not key or not url:
                    continue
                cols = s.get("columns")
                if isinstance(cols, str):
                    cols = [c.strip() for c in cols.split(",") if c.strip()]
                if not isinstance(cols, list) or not cols:
                    cols = ["day", "artist", "album"]
                fallback_titles = s.get("fallback_titles") or []
                if isinstance(fallback_titles, str):
                    fallback_titles = [fallback_titles]
                loaded[key] = {
                    "url": url,
                    "name": str(s.get("name", key)),
                    "columns": list(cols),
                    "fallback_titles": list(fallback_titles),
                }
            if loaded:
                logger.debug("[SCRAPER] Loaded %d sources from config", len(loaded))
                return loaded
        except Exception as exc:
            logger.warning("[SCRAPER] Could not load configured sources: %s", exc)
        return DEFAULT_WIKIPEDIA_SOURCES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scrape_all(self) -> dict[str, Any]:
        """Scrape all configured Wikipedia sources.

        Returns:
            Dict with per-source results and aggregate counts.
        """
        results: dict[str, Any] = {
            "total_found": 0,
            "total_new": 0,
            "total_updated": 0,
            "sources": {},
        }

        for src_key, src_info in self.sources.items():
            try:
                items = self._scrape_source(src_key, src_info)
            except Exception as exc:
                logger.error("[SCRAPER] Failed to scrape %s: %s", src_info["name"], exc)
                results["sources"][src_key] = {
                    "name": src_info["name"],
                    "error": str(exc),
                    "items_found": 0,
                }
                continue

            new, updated = self._persist_releases(items, src_info["name"])

            results["total_found"] += len(items)
            results["total_new"] += new
            results["total_updated"] += updated
            results["sources"][src_key] = {
                "name": src_info["name"],
                "items_found": len(items),
                "items_new": new,
                "items_updated": updated,
            }

        return results

    # ------------------------------------------------------------------
    # Source scraping
    # ------------------------------------------------------------------

    def _scrape_source(self, src_key: str, src_info: dict[str, Any]) -> list[dict[str, Any]]:
        """Scrape a single Wikipedia source page.

        Returns:
            List of release dicts with artist_name, album_name, release_date, etc.
        """
        url = src_info["url"]
        name = src_info["name"]
        column_order = src_info.get("columns", ["day", "artist", "album"])
        year = self._extract_year(src_key)
        logger.debug("[SCRAPER] Scraping %s (year=%s)", name, year)

        # Fetch page HTML via Wikipedia API, trying fallback pages if needed
        page_titles_to_try = [self._url_to_page_title(url)]
        fallback_titles = src_info.get("fallback_titles", [])
        for ft in fallback_titles:
            if ft not in page_titles_to_try:
                page_titles_to_try.append(ft)

        parse_data = None
        used_title = None
        for pt in page_titles_to_try:
            parse_data = self.http.parse_page(pt, prop="text")
            if parse_data:
                used_title = pt
                logger.debug("[SCRAPER] Found content on page: %s", pt)
                break
            logger.debug("[SCRAPER] Page not found, trying fallback: %s", pt)

        if not parse_data:
            logger.warning("[SCRAPER] No parse data for %s (tried %d titles)", name, len(page_titles_to_try))
            return []

        raw_html = parse_data.get("text", {}).get("*", "")
        if not raw_html:
            logger.warning("[SCRAPER] No HTML content for %s (title=%s)", name, used_title)
            return []

        return self._parse_html_tables(raw_html, src_key, name, year, column_order)

    # ------------------------------------------------------------------
    # HTML table parsing
    # ------------------------------------------------------------------

    def _parse_html_tables(
        self,
        html: str,
        src_key: str,
        src_name: str,
        year: int,
        column_order: list[str],
    ) -> list[dict[str, Any]]:
        """Parse Wikipedia HTML tables to extract release data."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("[SCRAPER] BeautifulSoup not installed, falling back to regex")
            return self._parse_regex_fallback(html, year, column_order)

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table", class_="wikitable")
        releases: list[dict[str, Any]] = []
        current_month = 1
        last_seen_day: int | None = None

        for table in tables:
            # Detect month from preceding heading; skip tables that are not
            # under a month heading (award ceremonies, charts, etc.).
            current_month, last_seen_day, found = self._detect_month(table, current_month, last_seen_day)
            if not found:
                continue

            rows = table.find_all("tr")
            if not rows:
                continue

            # Skip header rows
            data_rows = self._skip_header_rows(rows)
            # Reconstruct with rowspan/colspan handling
            reconstructed = self._reconstruct_rows(data_rows)

            for cells in reconstructed:
                if len(cells) < 2:
                    continue
                release = self._parse_row(
                    cells, column_order, year, current_month, last_seen_day
                )
                if release:
                    releases.append(release)
                    if release.get("_had_date"):
                        try:
                            day = int(release["release_date"].split("-")[2])
                            last_seen_day = day
                        except (ValueError, IndexError):
                            pass

        logger.info("[SCRAPER] Parsed %s releases from %s", len(releases), src_name)
        return releases

    def _detect_month(
        self, table: Any, current_month: int, last_seen_day: int | None
    ) -> tuple[int, int | None, bool]:
        """Walk backwards from a table to find the month heading.

        Returns ``(month, last_seen_day, found)`` — ``found`` is False when no
        month heading precedes the table, which marks it as a non-release
        table (award ceremonies, charts, references, etc.).
        """
        try:
            prev = table.find_previous()
            depth = 0
            while prev and depth < 50:
                text = prev.get_text(strip=True).lower()
                for mname, mnum in _MONTHS.items():
                    if mname in text and len(text) < 100:
                        logger.debug("[SCRAPER] Month heading: %s -> %s", prev.get_text(strip=True), mnum)
                        # Extract day if present (e.g. "January 9")
                        day_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
                        if day_match:
                            d = int(day_match.group(1))
                            if 1 <= d <= 31:
                                last_seen_day = d
                        return mnum, last_seen_day, True
                if prev.name == "table":
                    break
                prev = prev.find_previous()
                depth += 1
        except Exception:
            pass
        return current_month, last_seen_day, False

    @staticmethod
    def _skip_header_rows(rows: list[Any]) -> list[Any]:
        """Skip header rows (rows with mostly TH elements)."""
        start = 0
        for i, row in enumerate(rows[:5]):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            th_count = sum(1 for c in cells if c.name == "th")
            td_count = sum(1 for c in cells if c.name == "td")
            if th_count >= 3 and th_count > td_count:
                start = i + 1
            else:
                break
        return rows[start:]

    @staticmethod
    def _reconstruct_rows(rows: list[Any]) -> list[list[str]]:
        """Reconstruct rows handling rowspan/colspan properly."""
        reconstructed: list[list[str]] = []
        col_tracking: dict[int, tuple[str, int]] = {}

        for row in rows:
            cells = row.find_all(["td", "th"])
            row_cells: list[str] = []
            col_idx = 0

            for cell in cells:
                # Fill in tracked rowspan cells
                while col_idx in col_tracking and col_tracking[col_idx][1] > 0:
                    row_cells.append(col_tracking[col_idx][0])
                    col_tracking[col_idx] = (
                        col_tracking[col_idx][0],
                        col_tracking[col_idx][1] - 1,
                    )
                    col_idx += 1

                value = cell.get_text(strip=True)
                row_cells.append(value)
                rowspan = int(cell.get("rowspan", 1))
                colspan = int(cell.get("colspan", 1))

                if rowspan > 1:
                    col_tracking[col_idx] = (value, rowspan - 1)
                col_idx += colspan

            # Fill remaining tracked cells
            while col_idx in col_tracking and col_tracking[col_idx][1] > 0:
                row_cells.append(col_tracking[col_idx][0])
                col_tracking[col_idx] = (
                    col_tracking[col_idx][0],
                    col_tracking[col_idx][1] - 1,
                )
                col_idx += 1

            if row_cells:
                reconstructed.append(row_cells)

        return reconstructed

    def _parse_row(
        self,
        cells: list[str],
        column_order: list[str],
        year: int,
        current_month: int,
        last_seen_day: int | None,
    ) -> dict[str, Any] | None:
        """Parse a single row of cells into a release dict."""
        # Clean citation brackets
        cells = [re.sub(r"\s*\[\d+\]\s*", " ", c).strip() for c in cells]

        # Detect if first cell is a date
        first = cells[0] if cells else ""
        has_date = self._looks_like_date_cell(first)
        actual_cols = column_order.copy()

        if "day" in actual_cols and not has_date:
            actual_cols.remove("day")

        # Map columns to values
        values: dict[str, str] = {}
        ci = 0
        for col_type in actual_cols:
            if ci >= len(cells):
                break
            val = cells[ci].strip()
            if not val:
                ci += 1
                continue
            if col_type == "genre":
                ci += 1
                continue
            if val.upper() in ("TBA", "TBD", "TBR") or len(val) < 2:
                ci += 1
                continue
            if self._is_genre(val):
                ci += 1
                continue
            values[col_type] = val
            ci += 1

        artist = values.get("artist")
        album = values.get("album")
        if not artist or not album:
            return None

        # Parse day; if the cell also carries a month name (e.g. "January 9"
        # or "January9"), prefer it over the heading-detected month.
        day_str = values.get("day")
        day = self._parse_day(day_str, last_seen_day)
        if day is None:
            day = 1
        row_month = self._month_in_cell(day_str) or current_month

        # Build date string
        try:
            release_dt = datetime(year, row_month, day)
        except ValueError:
            day = 1
            try:
                release_dt = datetime(year, row_month, day)
            except ValueError:
                return None

        release_date = release_dt.strftime("%Y-%m-%d")

        return {
            "artist_name": artist.strip(),
            "album_name": album.strip(),
            "release_date": release_date,
            "release_year": year,
            "_had_date": has_date,
        }

    # ------------------------------------------------------------------
    # Fallback regex parser
    # ------------------------------------------------------------------

    def _parse_regex_fallback(
        self, html: str, year: int, column_order: list[str]
    ) -> list[dict[str, Any]]:
        """Regex-based table row parsing when BeautifulSoup is unavailable.
        
        Respects column_order mapping instead of hardcoded positions.
        """
        releases: list[dict[str, Any]] = []
        # Match rows with at least 2 cells
        for match in re.finditer(
            r"<tr>(.*?)</tr>",
            html, re.DOTALL,
        ):
            row_html = match.group(1)
            # Extract all cell contents
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL)
            # Clean HTML tags and citation brackets
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            cells = [re.sub(r"\s*\[\d+\]\s*", " ", c).strip() for c in cells]
            
            if len(cells) < 2:
                continue
                
            # Check if first cell is a date
            has_date = self._looks_like_date_cell(cells[0]) if cells else False
            actual_cols = column_order.copy()
            if "day" in actual_cols and not has_date:
                actual_cols.remove("day")
            
            # Map cells to column types
            values = {}
            for idx, col_type in enumerate(actual_cols):
                if idx < len(cells):
                    values[col_type] = cells[idx]
            
            # Extract artist, album, and date
            artist = values.get("artist", "")
            album = values.get("album", "")
            
            if not artist or not album or len(artist) > 200:
                continue
            
            # Parse date if present
            if "day" in values:
                day = self._parse_day(values["day"], None)
                month = self._month_in_cell(values["day"]) or 1
                if day:
                    date_str = f"{year}-{month:02d}-{day:02d}"
                else:
                    date_str = f"{year}-{month:02d}-01"
                had_date = bool(day)
            else:
                date_str = f"{year}-01-01"
                had_date = False
            
            releases.append({
                "artist_name": artist,
                "album_name": album,
                "release_date": date_str,
                "release_year": year,
                "_had_date": had_date,
            })
        
        return releases

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _url_to_page_title(url: str) -> str:
        """Extract Wikipedia page title from URL."""
        if "en.wikipedia.org/wiki/" in url:
            title = url.split("wiki/")[-1].split("#")[0]
            return title
        return "List_of_upcoming_albums"

    @staticmethod
    def _extract_year(src_key: str) -> int:
        """Extract year from source key (e.g. '2026_albums' -> 2026)."""
        match = re.search(r"(20[2-9]\d)", src_key)
        return int(match.group(1)) if match else datetime.now().year

    @staticmethod
    def _parse_day(day_str: str | None, last_seen: int | None) -> int | None:
        """Parse day number from a string."""
        if not day_str:
            return last_seen
        match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?", day_str)
        if match:
            d = int(match.group(1))
            if 1 <= d <= 31:
                return d
        return last_seen

    _MONTH_NAME_RE = re.compile(
        r"(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)",
        re.IGNORECASE,
    )
    _BARE_DAY_RE = re.compile(r"\d{1,2}(?:st|nd|rd|th)?\.?")

    @classmethod
    def _looks_like_date_cell(cls, text: str) -> bool:
        """True when a cell is a release date: a month name + day, or a bare day number."""
        if not text:
            return False
        if cls._MONTH_NAME_RE.search(text):
            return True
        return bool(cls._BARE_DAY_RE.fullmatch(text.strip()))

    @classmethod
    def _month_in_cell(cls, text: str) -> int | None:
        """Extract a month number from a date cell (e.g. 'January 9' -> 1)."""
        m = cls._MONTH_NAME_RE.search(text or "")
        if m:
            return _MONTHS[m.group(1).lower()]
        return None

    @staticmethod
    def _is_genre(val: str) -> bool:
        """Detect if a cell value looks like genre metadata instead of artist/album."""
        if "," in val:
            parts = [p.strip().lower() for p in val.split(",")]
            if len(parts) >= 2:
                return any(
                    any(kw in p for kw in _GENRE_KEYWORDS)
                    for p in parts[:2]
                )
        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _persist_releases(
        releases: list[dict[str, Any]], source_name: str
    ) -> tuple[int, int]:
        """Save scraped releases to the database.

        Returns:
            Tuple of (new_count, updated_count).
        """
        new_count = 0
        updated_count = 0

        if not releases:
            return 0, 0

        with db_session() as session:
            for release in releases:
                artist = release["artist_name"]
                album = release["album_name"]
                rel_date = release["release_date"]

                # Upsert (match schema: artist_name, album_name, source, release_date)
                try:
                    result = session.execute(
                        text("""
                            INSERT INTO upcoming_releases
                                (artist_name, album_name, source, release_date)
                            VALUES (:artist, :album, :source, :date)
                            ON CONFLICT (artist_name, album_name, source)
                            DO UPDATE SET
                                release_date = EXCLUDED.release_date,
                                updated_at = CURRENT_TIMESTAMP
                        """),
                        {
                            "artist": artist,
                            "album": album,
                            "date": rel_date,
                            "source": source_name,
                        },
                    )
                    if result.rowcount == 1:
                        new_count += 1
                    else:
                        updated_count += 1
                except Exception as exc:
                    logger.debug("[SCRAPER] Upsert failed for %s - %s: %s", artist, album, exc)

        logger.info(
            "[SCRAPER] %s: %s new, %s updated",
            source_name, new_count, updated_count,
        )
        return new_count, updated_count


# ------------------------------------------------------------------
# Convenience wrapper
# ------------------------------------------------------------------

def scrape(
    sources: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Scrape all configured Wikipedia sources.

    Args:
        sources: Optional dict of source configurations. Falls back to
                 DEFAULT_WIKIPEDIA_SOURCES.

    Returns:
        Results dict with per-source details.
    """
    scraper = WikipediaReleaseScraper(sources=sources)
    return scraper.scrape_all()
