#!/usr/bin/env python3
"""
Discogs Singles/EPs Cache Manager

Manages caching of Singles & EPs releases for each artist from Discogs.
This allows fast track-title lookups without repeated API calls.

Features:
- Title normalization (lowercase, strip punctuation, curly quotes)
- TTL-based cache expiration (7-30 days configurable)
- Database persistence
- Prevents repeated API calls during same scan
"""

import logging
import re
import time
from typing import Dict, Set, Tuple, Optional, List
from datetime import datetime, timedelta
from helpers.db_utils import get_db_connection

logger = logging.getLogger(__name__)

def normalize_track_title(title: str) -> str:
    """
    Normalize track title for cache lookups.
    
    Rules:
    - Convert curly quotes to straight quotes
    - Lowercase all text
    - Remove ALL punctuation (including periods in acronyms like M.M.I.X.)
    - Collapse multiple whitespace to single space
    - Strip leading/trailing whitespace
    
    Examples:
    - "M.M.I.X." → "mmix"
    - "Life in Technicolor ii" → "life in technicolor ii"
    - "Song (Remix)" → "song remix"
    - "Don't Stop" → "dont stop"
    - "Café" → "cafe"
    
    Args:
        title: Original track title
        
    Returns:
        Normalized title for consistent matching
    """
    if not title:
        return ""
    
    # Convert curly quotes to straight quotes
    title = title.replace('"', '"').replace('"', '"')
    title = title.replace("'", "'").replace("'", "'")
    
    # Lowercase
    title = title.lower()
    
    # Remove ASCII punctuation but keep unicode letters/numbers/spaces
    # This removes periods, dashes, commas, parentheses, etc.
    title = re.sub(r'[^\w\s]', '', title, flags=re.UNICODE)
    
    # Collapse multiple whitespace to single space
    title = re.sub(r'\s+', ' ', title)
    
    # Strip leading/trailing whitespace
    title = title.strip()
    
    return title


class DiscogsArtistSinglesCache:
    """
    Manages caching of Singles & EPs track titles for each artist from Discogs.
    """
    
    def __init__(self, db_path: str, cache_ttl_days: int = 7):
        """
        Initialize the cache manager.
        
        Args:
            db_path: Path to database file
            cache_ttl_days: Cache time-to-live in days (default 7, max 30)
        """
        self.db_path = db_path
        self.cache_ttl_days = min(cache_ttl_days, 30)  # Cap at 30 days
        self.placeholder = "%s"
        
        self._ensure_schema()

    def _get_connection(self):
        """Get PostgreSQL connection for cache operations."""
        return get_db_connection()
    
    def _ensure_schema(self):
        """Ensure database schema exists."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Create table for caching artist Singles/EPs
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS discogs_singles_cache (
                    id BIGSERIAL PRIMARY KEY,
                    artist TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    original_title TEXT,
                    release_id INTEGER,
                    release_year INTEGER,
                    is_official BOOLEAN DEFAULT TRUE,
                    is_promo BOOLEAN DEFAULT FALSE,
                    is_single BOOLEAN NOT NULL DEFAULT TRUE,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(artist, normalized_title)
                )
            """)

            # Add is_single column to existing installations that pre-date this column
            try:
                cursor.execute("ALTER TABLE discogs_singles_cache ADD COLUMN IF NOT EXISTS is_single BOOLEAN NOT NULL DEFAULT TRUE")
            except Exception:
                pass  # Column already exists

            # Ensure id column has a sequence default (migration for existing installations
            # that were created before the id BIGSERIAL column was added to the schema).
            try:
                cursor.execute("""
                    DO $$
                    DECLARE
                        v_has_id boolean;
                        v_has_default boolean;
                    BEGIN
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'discogs_singles_cache' AND column_name = 'id'
                        ) INTO v_has_id;

                        IF NOT v_has_id THEN
                            CREATE SEQUENCE IF NOT EXISTS discogs_singles_cache_id_seq;
                            ALTER TABLE discogs_singles_cache
                                ADD COLUMN id BIGINT DEFAULT nextval('discogs_singles_cache_id_seq');
                            UPDATE discogs_singles_cache
                                SET id = nextval('discogs_singles_cache_id_seq') WHERE id IS NULL;
                            ALTER TABLE discogs_singles_cache ALTER COLUMN id SET NOT NULL;
                        ELSE
                            SELECT column_default IS NOT NULL
                              INTO v_has_default
                              FROM information_schema.columns
                             WHERE table_name = 'discogs_singles_cache' AND column_name = 'id';

                            IF NOT v_has_default THEN
                                CREATE SEQUENCE IF NOT EXISTS discogs_singles_cache_id_seq;
                                PERFORM setval(
                                    'discogs_singles_cache_id_seq',
                                    COALESCE((SELECT MAX(id) FROM discogs_singles_cache), 0) + 1
                                );
                                ALTER TABLE discogs_singles_cache
                                    ALTER COLUMN id SET DEFAULT nextval('discogs_singles_cache_id_seq');
                                UPDATE discogs_singles_cache
                                    SET id = nextval('discogs_singles_cache_id_seq') WHERE id IS NULL;
                            END IF;
                        END IF;
                    END $$;
                """)
            except Exception:
                pass  # Migration already applied or not needed

            # Add unique constraint for ON CONFLICT support on existing installations that pre-date this constraint
            try:
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conrelid = 'discogs_singles_cache'::regclass
                              AND contype = 'u'
                              AND conname = 'discogs_singles_cache_artist_normalized_title_key'
                        ) THEN
                            ALTER TABLE discogs_singles_cache
                                ADD CONSTRAINT discogs_singles_cache_artist_normalized_title_key
                                UNIQUE (artist, normalized_title);
                        END IF;
                    END
                    $$;
                """)
            except Exception:
                pass  # Constraint already exists or table uses a different name

            # Create index for fast lookups
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_discogs_singles_artist
                ON discogs_singles_cache(artist, normalized_title)
            """)
            
            # Create table for tracking last cache refresh per artist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS discogs_cache_metadata (
                    artist TEXT PRIMARY KEY,
                    last_cached_at TIMESTAMP,
                    total_tracks INTEGER DEFAULT 0
                )
            """)
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to initialize Discogs cache schema: {e}")
    
    def _is_cache_expired(self, artist: str) -> bool:
        """Check if cache for artist is expired."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(f"""
                SELECT last_cached_at FROM discogs_cache_metadata
                WHERE artist = {self.placeholder}
            """, (artist,))
            
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                return True  # No cache entry = expired

            last_cached = row.get('last_cached_at') if isinstance(row, dict) else row[0]
            if isinstance(last_cached, str):
                last_cached = datetime.fromisoformat(last_cached.replace('Z', '+00:00')).replace(tzinfo=None)
            if not isinstance(last_cached, datetime):
                return True
            expiry = last_cached + timedelta(days=self.cache_ttl_days)
            return datetime.now() > expiry
        
        except Exception as e:
            logger.warning(f"Error checking cache expiry for '{artist}': {e}")
            return True  # Assume expired on error
    
    def get_cached_titles(self, artist: str) -> Set[str]:
        """
        Get all normalized track titles from cache for artist.
        
        Args:
            artist: Artist name
            
        Returns:
            Set of normalized track titles, empty if not cached or expired
        """
        if self._is_cache_expired(artist):
            return set()
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(f"""
                SELECT normalized_title FROM discogs_singles_cache
                WHERE artist = {self.placeholder}
            """, (artist,))
            
            titles = {row[0] for row in cursor.fetchall()}
            conn.close()
            return titles
        
        except Exception as e:
            logger.warning(f"Error retrieving cache for '{artist}': {e}")
            return set()
    
    def get_cached_entry(self, artist: str, normalized_title: str) -> Optional[Dict]:
        """
        Get full cache entry for a track.
        
        Returns:
            Dict with original_title, release_id, release_year, is_official, is_promo
            or None if not found
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(f"""
                SELECT original_title, release_id, release_year, is_official, is_promo
                FROM discogs_singles_cache
                WHERE artist = {self.placeholder} AND normalized_title = {self.placeholder}
            """, (artist, normalized_title))
            
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                return None
            
            return {
                'original_title': row[0],
                'release_id': row[1],
                'release_year': row[2],
                'is_official': bool(row[3]),
                'is_promo': bool(row[4]),
            }
        
        except Exception as e:
            logger.warning(f"Error retrieving cache entry: {e}")
            return None
    
    def add_to_cache(self, artist: str, tracks: List[Tuple[str, Optional[int], Optional[int], bool, bool]]) -> int:
        """
        Add tracks to cache for an artist (replaces existing cache).
        
        Args:
            artist: Artist name
            tracks: List of (original_title, release_id, release_year, is_official, is_promo)
        
        Returns:
            Number of tracks added
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Clear existing cache for this artist
            cursor.execute(f"DELETE FROM discogs_singles_cache WHERE artist = {self.placeholder}", (artist,))
            
            # Add new tracks
            added = 0
            for original_title, release_id, release_year, is_official, is_promo in tracks:
                normalized_title = normalize_track_title(original_title)
                cursor.execute(f"""
                    INSERT INTO discogs_singles_cache
                    (artist, normalized_title, original_title, release_id, release_year, is_official, is_promo)
                    VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder})
                    ON CONFLICT(artist, normalized_title) DO UPDATE SET
                    original_title = EXCLUDED.original_title,
                    release_id = EXCLUDED.release_id,
                    release_year = EXCLUDED.release_year,
                    is_official = EXCLUDED.is_official,
                    is_promo = EXCLUDED.is_promo,
                    cached_at = CURRENT_TIMESTAMP
                """, (artist, normalized_title, original_title, release_id, release_year, is_official, is_promo))
                
                added += 1
            
            # Update metadata
            cursor.execute(f"""
                INSERT INTO discogs_cache_metadata
                (artist, last_cached_at, total_tracks)
                VALUES ({self.placeholder}, CURRENT_TIMESTAMP, {self.placeholder})
                ON CONFLICT(artist) DO UPDATE SET
                last_cached_at = CURRENT_TIMESTAMP,
                total_tracks = EXCLUDED.total_tracks
            """, (artist, len(tracks)))
            
            conn.commit()
            conn.close()
            
            logger.info(f"Cached {added} track(s) from Discogs Singles/EPs for artist '{artist}'")
            return added
        
        except Exception as e:
            logger.error(f"Error adding tracks to cache: {e}")
            return 0

    def get_cached_result(self, artist: str, normalized_title: str, ttl_hours: int = 168) -> Optional[bool]:
        """
        Get a cached single-detection result for a specific (artist, title) pair.

        Returns:
            True  — track was confirmed as a Discogs single/EP
            False — track was checked and confirmed NOT a Discogs single/EP
            None  — no cached result (not yet checked, or cache expired)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT is_single, cached_at
                FROM discogs_singles_cache
                WHERE artist = {self.placeholder} AND normalized_title = {self.placeholder}
            """, (artist, normalized_title))
            row = cursor.fetchone()
            conn.close()
            if not row:
                return None
            cached_at = row[1] if not isinstance(row, dict) else row.get('cached_at')
            if cached_at:
                # Normalise to a naive UTC datetime for age comparison
                if isinstance(cached_at, str):
                    cached_at = datetime.fromisoformat(cached_at.replace('Z', '+00:00'))
                if hasattr(cached_at, 'tzinfo') and cached_at.tzinfo is not None:
                    # Convert aware datetime to naive UTC
                    import calendar
                    cached_at = datetime.utcfromtimestamp(calendar.timegm(cached_at.utctimetuple()))
                if (datetime.utcnow() - cached_at) > timedelta(hours=ttl_hours):
                    return None  # Expired
            is_single_val = row[0] if not isinstance(row, dict) else row.get('is_single')
            return bool(is_single_val)
        except Exception as e:
            logger.warning(f"Error reading cached result for '{artist}'/'{normalized_title}': {e}")
            return None

    def save_result(self, artist: str, original_title: str, is_single: bool) -> None:
        """
        Persist a single-detection result (positive or negative) for future lookups.

        Args:
            artist: Artist name
            original_title: Original track title (will be normalized for storage)
            is_single: True if confirmed as a Discogs single/EP, False otherwise
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            normalized = normalize_track_title(original_title)
            if not normalized:
                conn.close()
                return
            cursor.execute(f"""
                INSERT INTO discogs_singles_cache
                    (artist, normalized_title, original_title, is_single, cached_at)
                VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, CURRENT_TIMESTAMP)
                ON CONFLICT(artist, normalized_title) DO UPDATE SET
                    is_single = EXCLUDED.is_single,
                    original_title = EXCLUDED.original_title,
                    cached_at = CURRENT_TIMESTAMP
            """, (artist, normalized, original_title, is_single))
            conn.commit()
            conn.close()
            logger.debug(f"Saved Discogs result for '{artist}'/'{original_title}': is_single={is_single}")
        except Exception as e:
            logger.warning(f"Error saving Discogs result for '{artist}'/'{original_title}': {e}")

    def clear_artist_cache(self, artist: str):
        """Clear cache for specific artist."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(f"DELETE FROM discogs_singles_cache WHERE artist = {self.placeholder}", (artist,))
            cursor.execute(f"DELETE FROM discogs_cache_metadata WHERE artist = {self.placeholder}", (artist,))
            
            conn.commit()
            conn.close()
            
            logger.info(f"Cleared Discogs cache for artist '{artist}'")
        
        except Exception as e:
            logger.error(f"Error clearing cache for '{artist}': {e}")
    
    def clear_expired_caches(self):
        """Remove expired cache entries."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Calculate cutoff date
            cutoff = datetime.now() - timedelta(days=self.cache_ttl_days)
            
            # Delete expired entries
            cursor.execute(f"""
                DELETE FROM discogs_singles_cache
                WHERE cached_at < {self.placeholder}
            """, (cutoff.isoformat(),))
            
            # Delete corresponding metadata
            cursor.execute(f"""
                DELETE FROM discogs_cache_metadata
                WHERE last_cached_at < {self.placeholder}
            """, (cutoff.isoformat(),))
            
            removed = cursor.rowcount
            conn.commit()
            conn.close()
            
            if removed > 0:
                logger.info(f"Removed {removed} expired Discogs cache entries")
        
        except Exception as e:
            logger.error(f"Error clearing expired caches: {e}")


# Global cache instance
_discogs_cache_instance: Optional[DiscogsArtistSinglesCache] = None


def get_discogs_cache(db_path: str = None, cache_ttl_days: int = 7) -> DiscogsArtistSinglesCache:
    """Get or create singleton cache instance."""
    global _discogs_cache_instance
    
    if _discogs_cache_instance is None:
        # db_path is accepted for legacy compatibility but the class uses get_db_connection()
        # for all operations, so an empty string is fine when no explicit path is given.
        _discogs_cache_instance = DiscogsArtistSinglesCache(db_path or "", cache_ttl_days)
    
    return _discogs_cache_instance
