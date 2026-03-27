#!/usr/bin/env python3
"""
Cover song detection module for automatic identification and attribution.

Detects cover songs by analyzing songwriter/composer data from MusicBrainz,
then attributes the original artist and updates track metadata accordingly.
"""

import logging
import json
import re
import time
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from api_clients.musicbrainz import _escape_lucene_special_chars as _esc

logger = logging.getLogger(__name__)

# Optional rate limiter – mirrors the pattern used in api_clients/musicbrainz.py
try:
    from helpers.api_rate_limiter import get_rate_limiter as _get_rate_limiter
    _rate_limiter = _get_rate_limiter()
except Exception:
    _rate_limiter = None


class CoverDetector:
    """Detect and attribute cover songs using MusicBrainz writer/composer data."""
    
    @staticmethod
    def _is_postgres(conn):
        """Detect if connection is PostgreSQL."""
        try:
            import psycopg2
            underlying = getattr(conn, "_conn", conn)
            return isinstance(underlying, psycopg2.extensions.connection)
        except (ImportError, AttributeError):
            return False
    
    def __init__(self, musicbrainz_client, db_connection=None):
        """
        Initialize cover detector.
        
        Args:
            musicbrainz_client: MusicBrainzClient instance for API queries
            db_connection: SQLite database connection
        """
        self.mb_client = musicbrainz_client
        self.db_conn = db_connection
        self.is_pg = self._is_postgres(db_connection) if db_connection else False
        self.placeholder = "%s"
        self._band_members_cache = {}  # Cache to avoid repeated API calls

    def _mb_api_get(self, path: str, params: dict) -> dict:
        """Make a rate-limited GET request to the MusicBrainz JSON API.

        Uses the shared ``self.mb_client`` session (which already has retry
        logic for 429/503) so we respect the app-wide rate-limiter.

        Args:
            path: Path relative to ``https://musicbrainz.org/ws/2/`` (no
                  leading slash), e.g. ``"recording/abc-123"`` or
                  ``"recording"`` (for search/browse).
            params: Query parameters dict.  ``fmt=json`` is added automatically.

        Returns:
            Parsed JSON dict, or empty dict on error.
        """
        if not self.mb_client:
            return {}
        params = dict(params)
        params.setdefault("fmt", "json")
        # Respect rate limiter if available
        if _rate_limiter:
            try:
                _rate_limiter.wait_if_needed_musicbrainz(max_wait_seconds=2.0)
                _rate_limiter.record_musicbrainz_request()
            except Exception:
                time.sleep(1.0)
        else:
            time.sleep(1.0)
        try:
            url = f"{self.mb_client.base_url}{path}"
            r = self.mb_client.session.get(url, params=params,
                                            headers=self.mb_client.headers,
                                            timeout=(5, 15))
            if not r.ok:
                logger.debug(f"[CoverDetector] MusicBrainz {r.status_code} for {url} params={params}")
                return {}
            return r.json() or {}
        except Exception as e:
            logger.debug(f"[CoverDetector] API error for {path}: {e}")
            return {}
    
    def detect_covers_for_album(self, album: str, artist: str, tracks: List[Dict]) -> List[Dict]:
        """
        Detect cover songs in an album by analyzing writer information.
        
        Logic:
        - For each track, check if the writer/composer is different from album artist
        - If a writer appears on only ONE track in the album, it's likely a cover
        - Look up the earliest recording by that writer on MusicBrainz
        - Return cover attribution information
        
        Args:
            album: Album name
            artist: Album artist
            tracks: List of track dicts with 'id', 'title', 'mbid', etc.
            
        Returns:
            List of dicts with cover detection results:
            {
                'track_id': str,
                'title': str,
                'is_cover': bool,
                'original_artist': str,
                'original_year': int,
                'writer': str,
                'confidence': str ('high'|'medium'|'low')
            }
        """
        logger.info(f"Starting cover detection for album '{album}' by '{artist}' ({len(tracks)} tracks)")
        
        # Step 1: Collect writer information for all tracks
        track_writers = {}
        for track in tracks:
            writers = self._get_track_writers(track)
            track_title = track.get('title', 'Unknown')
            if writers:
                track_writers[track['id']] = {
                    'title': track_title,
                    'writers': writers,
                    'mbid': track.get('mbid')
                }
                logger.debug(f"  Track '{track_title}': Found writers {writers}")
            else:
                logger.debug(f"  Track '{track_title}': No writer information in database")
        
        if not track_writers:
            logger.info(f"No writer information found for any tracks in album '{album}' - cover detection skipped")
            logger.info(f"  → To enable cover detection, ensure 'writer' field is populated from metadata sources during import")
            logger.info(f"  → Writer field should contain the original songwriter/composer name")
            return []
        
        # Step 2: Identify tracks with writers different from album artist (likely covers)
        # Any track whose writer/lyricist differs from the album artist is a candidate.
        cover_results = []
        seen_track_ids = set()  # Avoid processing same track twice
        for track_id, info in track_writers.items():
            if track_id in seen_track_ids:
                continue

            # Collect all writers that differ from the album artist in one pass so
            # _find_original_recording is called only once per track (not once per
            # writer), avoiding redundant MusicBrainz API calls.
            differing_writers = [
                w for w in info['writers']
                if not self._is_writer_same_as_artist(w, artist)
            ]

            if not differing_writers:
                continue

            logger.info(
                f"Potential cover: '{info['title']}' - "
                f"writer(s) [{', '.join(differing_writers)}] differ from artist '{artist}'"
            )

            # Look up original recording using all differing writers (tried in order).
            original = self._find_original_recording(
                info['title'],
                differing_writers,
                album_artist=artist,
                recording_mbid=info.get('mbid')
            )

            if original:
                result = {
                    'track_id': track_id,
                    'title': info['title'],
                    'is_cover': True,
                    'original_artist': original['artist'],
                    'original_year': original.get('year'),
                    'writer': ', '.join(differing_writers),
                    'confidence': original.get('confidence', 'medium')
                }
                cover_results.append(result)
                seen_track_ids.add(track_id)
                logger.info(f"✓ Cover confirmed: '{info['title']}' originally by '{original['artist']}' ({original.get('year', 'unknown year')})")

                # Get file path from track info if available
                track_data = next((t for t in tracks if t.get('id') == track_id), {})
                file_path = track_data.get('file_path')

                # Update database and file metadata
                self.update_cover_metadata(
                    track_id=track_id,
                    title=info['title'],
                    original_artist=original['artist'],
                    file_path=file_path
                )
            else:
                logger.debug(f"No original recording found for '{info['title']}' by writer(s) {differing_writers}")
        
        logger.info(f"Cover detection complete: found {len(cover_results)} covers in '{album}'")
        return cover_results
    
    def _get_track_writers(self, track: Dict) -> List[str]:
        """
        Extract writer/composer information from track data.
        
        Checks:
        1. Database 'writer' field (JSON array - primary source for songwriter info)
        2. MusicBrainz API (if MBID available and no local data)
        
        Args:
            track: Track dict with potential 'writer', 'mbid' fields
            
        Returns:
            List of writer names
        """
        writers = []
        
        # Check database writer field (JSON array format)
        if 'writer' in track and track['writer']: 
            try:
                if isinstance(track['writer'], str):
                    writers = json.loads(track['writer'])
                elif isinstance(track['writer'], list):
                    writers = track['writer']
            except json.JSONDecodeError:
                logger.debug(f"Could not parse writer field for track {track.get('title')}")
        
        # If no writers from DB, try MusicBrainz API
        if not writers and track.get('mbid'):
            writers = self._fetch_writers_from_musicbrainz(track['mbid'])
        
        return self._normalize_writer_credits(writers)
    
    def _normalize_writer_credits(self, writers: List[str]) -> List[str]:
        """Split combined writer credits and dedupe names."""
        normalized: List[str] = []
        for writer in writers or []:
            writer_text = str(writer or "").strip()
            if not writer_text:
                continue

            # Split common combined-credit separators while preserving plain names.
            parts = re.split(r"\s*[;/,&]|\s+and\s+\s*", writer_text, flags=re.IGNORECASE)
            for part in parts:
                name = re.sub(r"^\(+|\)+$", "", part.strip())
                if name and name not in normalized:
                    normalized.append(name)
        return normalized
    def _fetch_writers_from_musicbrainz(self, mbid: str) -> List[str]:
        """Fetch writer/composer credits for a recording from MusicBrainz.

        Uses the direct JSON API via :meth:`_mb_api_get` instead of the
        ``musicbrainzngs`` library so that it works without any extra
        dependency.

        Strategy:
        1. Look up the recording with ``inc=work-rels`` to get linked works.
        2. For each work, look it up with ``inc=artist-rels`` to get
           composer/writer/lyricist relationships.

        Args:
            mbid: MusicBrainz Recording ID.

        Returns:
            List of writer names.
        """
        try:
            # Step 1: recording lookup → work relationships
            rec_data = self._mb_api_get(f"recording/{mbid}", {"inc": "work-rels"})
            recording = rec_data.get("recording") or {}

            writers: List[str] = []
            for rel in recording.get("relations", []) or []:
                if rel.get("target-type") != "work":
                    continue
                work_id = (rel.get("work") or {}).get("id")
                if not work_id:
                    continue

                # Step 2: work lookup → artist relationships
                work_data = self._mb_api_get(f"work/{work_id}", {"inc": "artist-rels"})
                work = work_data.get("work") or {}
                for work_rel in work.get("relations", []) or []:
                    if work_rel.get("target-type") != "artist":
                        continue
                    rel_type = str(work_rel.get("type", "")).lower()
                    if rel_type in ("composer", "writer", "lyricist"):
                        name = (work_rel.get("artist") or {}).get("name")
                        if name and name not in writers:
                            writers.append(name)

            return writers

        except Exception as e:
            logger.debug(f"Failed to fetch writers from MusicBrainz for {mbid}: {e}")
            return []
    
    def _get_band_members(self, artist: str) -> List[str]:
        """
        Fetch band members for an artist from MusicBrainz.
        
        Caches results to avoid repeated API calls.
        
        Args:
            artist: Artist/band name
            
        Returns:
            List of band member names
        """
        # Check cache first
        if artist in self._band_members_cache:
            return self._band_members_cache[artist]

        try:
            members = []
            if self.mb_client and hasattr(self.mb_client, 'get_artist_member_names'):
                members = self.mb_client.get_artist_member_names(artist=artist)

            self._band_members_cache[artist] = members or []

            if members:
                logger.info(f"MusicBrainz found {len(members)} members for '{artist}': {', '.join(members)}")
            else:
                logger.debug(f"No band members found for '{artist}' in MusicBrainz")

            return self._band_members_cache[artist]
        except Exception as e:
            logger.debug(f"Failed to fetch band members for '{artist}' from MusicBrainz: {e}")
            self._band_members_cache[artist] = []
            return []
    
    def _is_writer_same_as_artist(self, writer: str, artist: str) -> bool:
        """
        Check if writer name matches the album artist (fuzzy matching).
        
        Also checks if the writer is a band member of the artist group.
        
        Args:
            writer: Writer/composer name
            artist: Album artist name
            
        Returns:
            True if they appear to be the same person/group or if writer is a band member
        """
        if self._names_match(writer, artist):
            return True

        # For groups, writer credit should match a known member to count as non-cover.
        band_members = self._get_band_members(artist)
        if band_members:
            for member in band_members:
                if self._names_match(writer, member):
                    logger.debug(f"Writer '{writer}' fuzzy-matched as band member '{member}' of '{artist}'")
                    return True
        
        return False

    @staticmethod
    def _normalize_name(value: str) -> str:
        """Normalize person/group names for robust matching."""
        if not value:
            return ""
        # Split CamelCase/PascalCase before lowercasing so e.g. "DiFiore" -> "Di Fiore"
        expanded = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', value)
        normalized = expanded.lower().strip()
        normalized = normalized.replace("’", "'")
        normalized = re.sub(r"\b(the|and)\b", " ", normalized)
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    def _extract_name_parts(self, value: str) -> Dict[str, str]:
        """Extract first token/initial and surname for fuzzy person matching."""
        normalized = self._normalize_name(value)
        if not normalized:
            return {"first": "", "first_initial": "", "last": ""}

        tokens = [tok for tok in normalized.split() if tok]
        if not tokens:
            return {"first": "", "first_initial": "", "last": ""}

        first = tokens[0]
        last = tokens[-1]
        return {
            "first": first,
            "first_initial": first[0] if first else "",
            "last": last,
        }

    def _names_match(self, left: str, right: str) -> bool:
        """Match names with token overlap to handle middle names and variants."""
        left_norm = self._normalize_name(left)
        right_norm = self._normalize_name(right)
        if not left_norm or not right_norm:
            return False
        if left_norm == right_norm:
            return True

        # Strong token overlap (ignores single-letter initials).
        left_tokens = {token for token in left_norm.split() if len(token) > 1}
        right_tokens = {token for token in right_norm.split() if len(token) > 1}
        if not left_tokens or not right_tokens:
            # Fall through to surname/initial matching below.
            left_tokens = {token for token in left_norm.split() if token}
            right_tokens = {token for token in right_norm.split() if token}

        intersection = left_tokens & right_tokens
        min_required = min(len(left_tokens), len(right_tokens))
        if len(intersection) >= max(2, min_required):
            return True

        # Accept surname-only credits if the surname is sufficiently specific.
        # Example: "Johnson" should match "Ryan Johnson" in band-member checks.
        if len(left_tokens) == 1 and len(right_tokens) >= 2:
            token = next(iter(left_tokens))
            if token in right_tokens and len(token) >= 4:
                return True
        if len(right_tokens) == 1 and len(left_tokens) >= 2:
            token = next(iter(right_tokens))
            if token in left_tokens and len(token) >= 4:
                return True

        # Match abbreviated first names with shared surname.
        # Example: "L. Cosby" vs "Lewis Cosby".
        left_parts = self._extract_name_parts(left)
        right_parts = self._extract_name_parts(right)
        if left_parts["last"] and right_parts["last"] and left_parts["last"] == right_parts["last"]:
            if left_parts["first"] == right_parts["first"]:
                return True
            if left_parts["first_initial"] and right_parts["first_initial"] and left_parts["first_initial"] == right_parts["first_initial"]:
                return True

        return False

    def _find_original_recording(
        self,
        title: str,
        writers: List[str],
        album_artist: Optional[str] = None,
        recording_mbid: Optional[str] = None
    ) -> Optional[Dict]:
        """Find the earliest likely original recording for a title/writers pair.

        Uses the MusicBrainz JSON API directly (no ``musicbrainzngs``
        dependency) via :meth:`_mb_api_get`.

        Approach
        --------
        1. If we have the scanned recording's MBID, look it up with
           ``inc=work-rels`` to get the canonical work IDs it belongs to.
        2. Otherwise search the *work* endpoint by title + each writer.
        3. For each matched work, browse all recordings
           (``GET /ws/2/recording?work=<id>&inc=artist-credits+releases``)
           in one request — much cheaper than the old per-recording lookup.
        4. Among the returned recordings, find the earliest one performed by
           an artist other than the album artist.
        """
        try:
            def _canonical_title(value: str) -> str:
                """Strip version suffixes then normalise."""
                text = str(value or "").strip()
                text = re.sub(r"\[[^\]]*\]", " ", text)
                text = re.sub(r"\([^)]*\)", " ", text)
                text = re.sub(r"\s+-\s+.*$", " ", text)
                return self._normalize_name(text)

            def _titles_match(left: str, right: str) -> bool:
                l_can = _canonical_title(left)
                r_can = _canonical_title(right)
                if not l_can or not r_can:
                    return False
                if l_can == r_can:
                    return True
                # Allow prefix match for minor suffix variants
                return l_can.startswith(r_can) or r_can.startswith(l_can)

            def _extract_credit_artist(artist_credit: List) -> str:
                for entry in artist_credit or []:
                    if isinstance(entry, dict):
                        name = (entry.get("artist") or {}).get("name") or entry.get("name")
                        if name:
                            return name
                return ""

            def _strip_suffixes_for_search(value: str) -> str:
                """Remove version suffixes for the MB search query."""
                text = str(value or "").strip()
                text = re.sub(r"\s*\[[^\]]*\]", "", text)
                text = re.sub(r"\s*\([^)]*\)", "", text)
                text = re.sub(r"\s+-\s+.*$", "", text)
                return text.strip() or str(value or "").strip()

            # ── Step 1: work IDs from scanned recording MBID ─────────────────
            target_work_ids: set = set()
            if recording_mbid:
                rec_data = self._mb_api_get(f"recording/{recording_mbid}", {"inc": "work-rels"})
                for rel in (rec_data.get("recording") or {}).get("relations", []) or []:
                    if rel.get("target-type") == "work":
                        wid = (rel.get("work") or {}).get("id")
                        if wid:
                            target_work_ids.add(wid)

            # ── Step 2: work search by title + writer ─────────────────────────
            matched_work_ids: set = set()
            if not target_work_ids:
                search_title = _strip_suffixes_for_search(title)
                for writer in writers:
                    if matched_work_ids:
                        break
                    esc_title = _esc(search_title)
                    esc_writer = _esc(writer)
                    work_data = self._mb_api_get(
                        "work",
                        {"query": f'work:"{esc_title}" AND artist:"{esc_writer}"',
                         "limit": 10},
                    )
                    for work in work_data.get("works", []) or []:
                        wid = work.get("id")
                        work_title = work.get("title", "")
                        # Both sides use _canonical_title so version suffixes
                        # on the work title (e.g. "Heroes (Single Edit)") still
                        # match the plain track title.
                        if wid and _canonical_title(work_title) == _canonical_title(title):
                            matched_work_ids.add(wid)

            work_ids = target_work_ids | matched_work_ids
            if not work_ids:
                return None

            # ── Step 3: browse recordings for each work ───────────────────────
            # Single browse request per work returns all recordings with
            # artist-credits and release dates — no per-recording lookup needed.
            earliest: Optional[Dict] = None
            earliest_year = 9999
            earliest_unknown: Optional[Dict] = None

            for work_id in list(work_ids)[:5]:
                browse_data = self._mb_api_get(
                    "recording",
                    {"work": work_id,
                     "inc": "artist-credits+releases",
                     "limit": 100},
                )
                for rec in browse_data.get("recordings", []) or []:
                    rec_title = rec.get("title", "")
                    if not _titles_match(title, rec_title):
                        continue

                    artist_credit = rec.get("artist-credit", []) or []
                    rec_artist = _extract_credit_artist(artist_credit)
                    if not rec_artist:
                        continue

                    # Skip if this is the album artist performing the song
                    if album_artist and self._names_match(rec_artist, album_artist):
                        continue

                    # Find earliest year across all releases for this recording
                    year: Optional[int] = None
                    for rel in rec.get("releases", []) or []:
                        date_str = str(rel.get("date", "") or "")
                        if len(date_str) >= 4 and date_str[:4].isdigit():
                            y = int(date_str[:4])
                            if year is None or y < year:
                                year = y
                    # Fall back to first-release-date on the recording itself
                    if year is None:
                        frd = str(rec.get("first-release-date", "") or "")
                        if len(frd) >= 4 and frd[:4].isdigit():
                            year = int(frd[:4])

                    if year is not None:
                        if year < earliest_year:
                            earliest_year = year
                            earliest = {
                                "artist": rec_artist,
                                "year": year,
                                "confidence": "high" if target_work_ids else "medium",
                            }
                    elif earliest_unknown is None:
                        earliest_unknown = {
                            "artist": rec_artist,
                            "year": None,
                            "confidence": "medium",
                        }

            return earliest or earliest_unknown

        except Exception as e:
            logger.debug(f"Failed to find original recording for '{title}' by {writers}: {e}")
            return None

    def update_cover_metadata(self, track_id: str, title: str, original_artist: str,
                            file_path: Optional[str] = None) -> bool:
        """Update track metadata to reflect cover attribution."""
        try:
            cover_suffix_pattern = re.compile(r'\s*\([^)]+\s+Cover\)\s*$', re.IGNORECASE)
            if cover_suffix_pattern.search(title):
                new_title = title
                logger.debug(f"Title '{title}' already has cover suffix, skipping title update")
            else:
                new_title = f"{title} ({original_artist} Cover)"

            if self.db_conn:
                cursor = self.db_conn.cursor()

                if new_title != title:
                    cursor.execute(
                        f"UPDATE tracks SET title = {self.placeholder} WHERE id = {self.placeholder}",
                        (new_title, track_id)
                    )

                cursor.execute(
                    f"SELECT genres FROM tracks WHERE id = {self.placeholder}",
                    (track_id,)
                )
                result = cursor.fetchone()
                if result:
                    # Support both dict-style (psycopg2 RealDictCursor) and
                    # index-style (sqlite3.Row) cursor results.
                    if hasattr(result, 'keys'):
                        current_genres = result['genres'] or ""
                    else:
                        current_genres = result[0] or ""
                    genres_list = [g.strip() for g in current_genres.split(",")] if current_genres else []
                    if "Cover" not in genres_list:
                        genres_list.append("Cover")
                    new_genres = ", ".join(genres_list)
                    cursor.execute(
                        f"UPDATE tracks SET genres = {self.placeholder} WHERE id = {self.placeholder}",
                        (new_genres, track_id)
                    )

                is_cover_value = True
                cursor.execute(
                    f"UPDATE tracks SET is_cover = {self.placeholder}, is_cover_reason = {self.placeholder}, original_cover_artist = {self.placeholder} WHERE id = {self.placeholder}",
                    (is_cover_value, f"Writer-based detection: original by {original_artist}", original_artist, track_id)
                )

                self.db_conn.commit()
                logger.info(f"✓ Database updated: '{title}' → '{new_title}' (original: {original_artist})")

            if file_path and Path(file_path).exists():
                self._update_file_metadata(file_path, new_title, ["Cover"])

            return True

        except Exception as e:
            logger.error(f"Failed to update cover metadata for track {track_id}: {e}")
            return False
    
    def _update_file_metadata(self, file_path: str, title: str, additional_genres: List[str]) -> bool:
        """
        Update audio file tags with cover attribution.
        
        Args:
            file_path: Path to MP3/FLAC file
            title: New title with cover attribution
            additional_genres: Genres to add (e.g., ["Cover"])
            
        Returns:
            True if successful
        """
        try:
            from mutagen.mp3 import MP3
            from mutagen.flac import FLAC
            from mutagen.id3 import ID3, TIT2, TCON
            
            path = Path(file_path)
            
            if path.suffix.lower() == '.mp3':
                audio = MP3(file_path, ID3=ID3)
                
                # Update title
                audio.tags['TIT2'] = TIT2(encoding=3, text=title)
                
                # Update genres
                current_genres = []
                if 'TCON' in audio.tags:
                    current_genres = list(audio.tags['TCON'].text)
                
                for genre in additional_genres:
                    if genre not in current_genres:
                        current_genres.append(genre)
                
                audio.tags['TCON'] = TCON(encoding=3, text=current_genres)
                
                audio.save()
                logger.info(f"✓ MP3 file updated: {path.name}")
                
            elif path.suffix.lower() == '.flac':
                audio = FLAC(file_path)
                
                # Update title
                audio['title'] = title
                
                # Update genres
                current_genres = audio.get('genre', [])
                if isinstance(current_genres, str):
                    current_genres = [current_genres]
                
                for genre in additional_genres:
                    if genre not in current_genres:
                        current_genres.append(genre)
                
                audio['genre'] = current_genres
                
                audio.save()
                logger.info(f"✓ FLAC file updated: {path.name}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update file metadata for {file_path}: {e}")
            return False
