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

import requests

from api_clients.musicbrainz import _VERSION as MUSICBRAINZ_VERSION

logger = logging.getLogger(__name__)

_MB_BASE_URL = "https://musicbrainz.org/ws/2"
_MB_HEADERS = {
    "User-Agent": f"sptnr/{MUSICBRAINZ_VERSION} (+https://github.com/M0VENTURA/sptnr)",
    "Accept": "application/json",
}
_MB_RATE_LIMIT = 1.1  # seconds between requests per MusicBrainz policy

# Matches a trailing "(X Cover)" annotation in a track title so it can be
# stripped before querying MusicBrainz or checking against the original title.
_COVER_SUFFIX_RE = re.compile(r'\s*\([^)]+\s+cover\)\s*$', re.IGNORECASE)


class _MusicBrainzRestClient:
    """
    REST-based MusicBrainz client for cover detection.

    Uses the MusicBrainz JSON API (v2) directly via *requests*, returning
    dicts whose structure matches what the cover-detection logic expects
    (i.e. the same field names used by musicbrainzngs XML responses).
    """

    def __init__(self) -> None:
        self._last_request: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Rate-limited GET to the MusicBrainz JSON API."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < _MB_RATE_LIMIT:
            time.sleep(_MB_RATE_LIMIT - elapsed)
        try:
            full_params = dict(params or {})
            full_params["fmt"] = "json"
            resp = requests.get(
                f"{_MB_BASE_URL}/{path}",
                params=full_params,
                headers=_MB_HEADERS,
                timeout=(6, 20),
            )
            self._last_request = time.monotonic()
            resp.raise_for_status()
            return resp.json() or {}
        except Exception as exc:
            logger.debug("MusicBrainz REST request failed for '%s': %s", path, exc)
            self._last_request = time.monotonic()
            return {}

    @staticmethod
    def _split_relations(data: dict) -> None:
        """
        Normalise the JSON API ``relations`` list (all relation types in one
        array) into the separate ``work-relation-list`` / ``artist-relation-list``
        / ``recording-relation-list`` arrays expected by the cover-detection
        logic.
        Mutates *data* in-place.
        """
        relations = data.pop("relations", None) or []
        work_rels: List[dict] = []
        artist_rels: List[dict] = []
        recording_rels: List[dict] = []
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            if isinstance(rel.get("work"), dict):
                work_rels.append(rel)
            elif isinstance(rel.get("artist"), dict):
                artist_rels.append(rel)
            elif isinstance(rel.get("recording"), dict):
                recording_rels.append(rel)
        data.setdefault("work-relation-list", work_rels)
        data.setdefault("artist-relation-list", artist_rels)
        data.setdefault("recording-relation-list", recording_rels)

    # ------------------------------------------------------------------
    # Public API (mirrors the subset of musicbrainzngs used here)
    # ------------------------------------------------------------------

    @staticmethod
    def _esc(value: str) -> str:
        """
        Escape a Lucene query value for safe embedding inside double-quotes.
        Only the double-quote and backslash characters need escaping when the
        value is wrapped in quotes (as in ``field:"<value>"``).
        """
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def set_useragent(self, *args, **kwargs) -> None:  # noqa: D401
        """No-op – user-agent is set globally via _MB_HEADERS."""

    def search_works(self, work: str = "", artist: str = "", limit: int = 25) -> dict:
        """
        Search for MusicBrainz works by title and composer/writer.
        Returns ``{"work-list": [...]}``.
        """
        parts: List[str] = []
        if work:
            parts.append(f'work:"{self._esc(work)}"')
        if artist:
            parts.append(f'artist:"{self._esc(artist)}"')
        query = " AND ".join(parts) if parts else "*"
        data = self._get("work", {"query": query, "limit": limit})
        return {"work-list": data.get("works", [])}

    def search_recordings(self, recording: str = "", limit: int = 25) -> dict:
        """
        Search for MusicBrainz recordings by title.
        Returns ``{"recording-list": [...]}``.
        """
        query = f'recording:"{self._esc(recording)}"' if recording else "*"
        data = self._get("recording", {"query": query, "limit": limit})
        return {"recording-list": data.get("recordings", [])}

    def get_recording_by_id(self, mbid: str, includes: Optional[List[str]] = None) -> dict:
        """
        Fetch a recording by MBID with optional includes.
        Returns ``{"recording": {...}}`` with normalised relation lists.
        """
        inc = "+".join(includes or [])
        data = self._get(f"recording/{mbid}", {"inc": inc} if inc else {})
        # Normalise: rename releases list and split relations
        if "releases" in data:
            data["release-list"] = data.pop("releases")
        self._split_relations(data)
        return {"recording": data}

    def get_work_by_id(self, work_id: str, includes: Optional[List[str]] = None) -> dict:
        """
        Fetch a work by ID with optional includes.
        Returns ``{"work": {...}}`` with normalised artist-relation-list.
        """
        inc = "+".join(includes or [])
        data = self._get(f"work/{work_id}", {"inc": inc} if inc else {})
        self._split_relations(data)
        return {"work": data}

    def get_release_by_id(self, release_id: str, includes: Optional[List[str]] = None) -> dict:
        """
        Fetch a release by ID with optional includes.
        Returns ``{"release": {...}}`` with normalised medium/track lists.
        """
        inc = "+".join(includes or [])
        data = self._get(f"release/{release_id}", {"inc": inc} if inc else {})
        # Normalise media list
        if "media" in data:
            media = data.pop("media") or []
            for medium in media:
                if "tracks" in medium:
                    medium["track-list"] = medium.pop("tracks")
            data["medium-list"] = media
        return {"release": data}


def _canonical_track_title(value: str) -> str:
    """Normalize track titles so album/version variants still match canonical recordings."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+-\s+.*$", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


class CoverDetector:
    """Detect and attribute cover songs using MusicBrainz relations and writer/composer data."""
    
    def __init__(self, musicbrainz_client, db_connection=None):
        """
        Initialize cover detector.
        
        Args:
            musicbrainz_client: MusicBrainzClient instance for API queries
            db_connection: database connection
        """
        self.mb_client = musicbrainz_client
        self.db_conn = db_connection
        self.placeholder = "%s"
        self._band_members_cache = {}  # Cache to avoid repeated API calls

    def _normalize_cover_flag_value(self, value: bool):
        """Normalize cover flags based on DB column type (BOOLEAN vs BIGINT/INTEGER)."""
        if not self.db_conn:
            return int(bool(value))

        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'tracks'
                  AND column_name = 'is_cover'
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if isinstance(row, dict):
                dtype = (row.get("data_type") or "").lower()
            elif row and len(row) > 0:
                dtype = (row[0] or "").lower()
            else:
                dtype = ""
            return bool(value) if dtype == "boolean" else int(bool(value))
        except Exception:
            # Be permissive if type introspection fails.
            return int(bool(value))

    @staticmethod
    def _row_value(row, key, index=0, default=None):
        """Read a value from dict-style or tuple-style DB rows."""
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        if hasattr(row, "keys"):
            try:
                return row[key]
            except Exception:
                pass
        try:
            return row[index]
        except Exception:
            return default

    @staticmethod
    def _is_deadlock_like(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "deadlock" in message
            or "could not serialize" in message
            or "infailedsqltransaction" in message
        )

    @staticmethod
    def _build_cover_title(title: str, original_artist: Optional[str]) -> str:
        cover_suffix_pattern = re.compile(r'\s*\([^)]+\s+Cover\)\s*$', re.IGNORECASE)
        if cover_suffix_pattern.search(title):
            return title
        if original_artist:
            return f"{title} ({original_artist} Cover)"
        return title

    def _apply_cover_metadata_batch(self, updates: List[Dict], max_retries: int = 5) -> List[str]:
        """Persist cover metadata updates in a deterministic batch to reduce lock churn."""
        if not self.db_conn or not updates:
            return []

        rows = sorted(
            [dict(update) for update in updates if update.get("track_id")],
            key=lambda item: str(item["track_id"]),
        )
        if not rows:
            return []

        is_cover_value = self._normalize_cover_flag_value(True)
        delay = 0.15

        for attempt in range(max_retries):
            try:
                cursor = self.db_conn.cursor()
                successful_track_ids = []

                for update in rows:
                    track_id = update["track_id"]
                    title = update.get("title", "")
                    original_artist = update.get("original_artist")
                    reason = update.get("is_cover_reason") or (
                        f"Writer-based detection: original by {original_artist}" if original_artist else "Writer-based detection"
                    )
                    new_title = self._build_cover_title(title, original_artist)

                    cursor.execute(
                        f"SELECT genres FROM tracks WHERE id = {self.placeholder}",
                        (track_id,)
                    )
                    result = cursor.fetchone()
                    current_genres = self._row_value(result, "genres", 0, "") or ""
                    genres_list = [genre.strip() for genre in current_genres.split(",") if genre.strip()]
                    if "Cover" not in genres_list:
                        genres_list.append("Cover")
                    new_genres = ", ".join(genres_list)

                    if new_title != title:
                        cursor.execute(
                            f"UPDATE tracks SET title = {self.placeholder} WHERE id = {self.placeholder}",
                            (new_title, track_id)
                        )

                    cursor.execute(
                        f"UPDATE tracks SET genres = {self.placeholder} WHERE id = {self.placeholder}",
                        (new_genres, track_id)
                    )
                    cursor.execute(
                        f"UPDATE tracks SET is_cover = {self.placeholder}, is_cover_reason = {self.placeholder}, original_cover_artist = {self.placeholder} WHERE id = {self.placeholder}",
                        (is_cover_value, reason, original_artist, track_id)
                    )
                    successful_track_ids.append(track_id)

                self.db_conn.commit()
                return successful_track_ids
            except Exception as exc:
                try:
                    self.db_conn.rollback()
                except Exception:
                    pass

                if self._is_deadlock_like(exc) and attempt < (max_retries - 1):
                    sleep_for = min(delay * (2 ** attempt), 2.0)
                    logger.warning(
                        f"Cover metadata batch hit transient DB contention ({type(exc).__name__}); "
                        f"retry {attempt + 1}/{max_retries} in {sleep_for:.2f}s"
                    )
                    time.sleep(sleep_for)
                    continue

                raise

        return []

    def _configure_mb_client(self) -> _MusicBrainzRestClient:
        """Return a MusicBrainz REST client for cover-detection lookups."""
        return _MusicBrainzRestClient()
    
    def detect_covers_for_album(self, album: str, artist: str, tracks: List[Dict]) -> List[Dict]:
        """
        Detect cover songs in an album using MusicBrainz relation signals first,
        then writer-based heuristics as fallback.
        
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

        for track in tracks:
            track.setdefault('album', album)
            track.setdefault('album_artist', artist)
        
        # Step 1: Collect writer information for all tracks
        track_writers = {}
        for track in tracks:
            writers = self._get_track_writers(track)
            track_title = track.get('title', 'Unknown')
            if writers:
                track_writers[track['id']] = {
                    'title': track_title,
                    'writers': writers,
                    'mbid': track.get('mbid'),
                    'track_artist': track.get('artist') or track.get('album_artist') or artist,
                }
                logger.debug(f"  Track '{track_title}': Found writers {writers}")
            else:
                logger.debug(f"  Track '{track_title}': No writer information in database")
        
        if not track_writers:
            logger.info(f"No writer information found for any tracks in album '{album}' - cover detection skipped")
            logger.info(f"  → To enable cover detection, ensure 'writer' field is populated from metadata sources during import")
            logger.info(f"  → Writer field should contain the original songwriter/composer name")
        
        cover_results = []
        seen_track_ids = set()  # Avoid processing same track twice
        pending_cover_updates = []

        # Step 2: Primary detection via MusicBrainz "cover recording of" relation.
        # This is the canonical signal shown in the MB UI and should take precedence.
        for track in tracks:
            track_id = track.get('id')
            if not track_id or track_id in seen_track_ids:
                continue

            mbid = track.get('mbid') or self._resolve_recording_mbid(
                track,
                album_artist=artist,
                album_title=album,
            )
            if not mbid:
                continue

            track['mbid'] = mbid

            relation_original = self._find_original_from_cover_relation(
                recording_mbid=mbid,
                title=track.get('title', ''),
                album_artist=artist
            )
            if not relation_original:
                continue

            # Guard: if the detected original artist is the same as the track's
            # credited artist, this recording is a cover *of* that artist's work —
            # not a cover *by* them.  Skip to avoid falsely flagging the artist's
            # own song as a cover of themselves.
            if self._names_match(relation_original.get('artist', ''), artist):
                logger.info(
                    f"Skipping MB-relation cover for '{track.get('title', '')}': "
                    f"original artist '{relation_original['artist']}' matches track artist "
                    f"'{artist}' — this is a cover OF the artist, not BY them"
                )
                continue

            result = {
                'track_id': track_id,
                'title': track.get('title', ''),
                'is_cover': True,
                'original_artist': relation_original['artist'],
                'original_year': relation_original.get('year'),
                'writer': '',
                'confidence': relation_original.get('confidence', 'high')
            }
            cover_results.append(result)
            seen_track_ids.add(track_id)
            logger.info(
                f"✓ Cover confirmed (MusicBrainz relation): '{track.get('title', '')}' "
                f"originally by '{relation_original['artist']}' ({relation_original.get('year', 'unknown year')})"
            )
            pending_cover_updates.append({
                'track_id': track_id,
                'title': track.get('title', ''),
                'original_artist': relation_original['artist'],
                'file_path': track.get('file_path'),
                'is_cover_reason': 'MusicBrainz cover relation',
            })

        # Step 3: Fallback detection from writer/lyricist mismatch + earliest recording lookup.
        # Any track whose writer/lyricist differs from the album artist is a candidate.
        for track_id, info in track_writers.items():
            if track_id in seen_track_ids:
                continue
            for writer in info['writers']:
                # Check if writer is different from album artist
                if not self._is_writer_same_as_artist(writer, artist):
                    logger.info(f"Potential cover: '{info['title']}' - lyricist/writer '{writer}' differs from artist '{artist}'")

                    # Before making a MusicBrainz lookup, verify the artist does
                    # not already own an original recording of this title in the
                    # local library.  This avoids flagging the artist's own songs
                    # as covers when their writer credit differs from the band name
                    # (e.g. a producer credit that doesn't match the artist name).
                    if self._artist_has_original(artist, info['title']):
                        logger.info(
                            f"Skipping writer-based cover detection for '{info['title']}': "
                            f"'{artist}' already has an original recording of this title in the library"
                        )
                        break

                    # If this writer appears on multiple tracks in the artist's
                    # catalogue they are a regular collaborator or producer, not
                    # evidence that a particular track is a cover song.
                    # On compilations the per-track artist (not the album artist
                    # "Various Artists") is the relevant lookup key.
                    if self._is_common_writer_for_artist(
                        writer, artist, track_artist=info.get('track_artist')
                    ):
                        logger.info(
                            f"Skipping writer-based cover detection for '{info['title']}': "
                            f"'{writer}' is a common writer in '{artist}' catalogue — "
                            f"likely a regular collaborator, not a cover indicator"
                        )
                        break

                    # Look up original recording by this writer.
                    # Strip any existing "(X Cover)" suffix from the title before
                    # querying MusicBrainz — otherwise the full annotated title
                    # (e.g. "Radioactive (Within Temptation Cover)") would be sent
                    # as the search query, returning poor results and causing the
                    # lookup to fail, which lets the Step 4 heuristic run and
                    # incorrectly adopt the embedded cover hint as the original artist.
                    _search_title = _COVER_SUFFIX_RE.sub('', info['title']).strip() or info['title']
                    original = self._find_original_recording(_search_title, writer, album_artist=artist)
                    
                    if original:
                        # The MusicBrainz lookup resolved the "original" recording
                        # to the album artist themselves (e.g. a co-writer search
                        # matched Brandy's own recording of her own song).  This is
                        # not a cover — skip it.
                        if self._names_match(original.get('artist', ''), artist):
                            logger.info(
                                f"Skipping writer-based cover detection for '{info['title']}': "
                                f"found original recording by '{original['artist']}' which matches "
                                f"album artist '{artist}' — not a cover"
                            )
                            break

                        result = {
                            'track_id': track_id,
                            'title': info['title'],
                            'is_cover': True,
                            'original_artist': original['artist'],
                            'original_year': original.get('year'),
                            'writer': writer,
                            'confidence': original.get('confidence', 'medium')
                        }
                        cover_results.append(result)
                        seen_track_ids.add(track_id)
                        logger.info(f"✓ Cover confirmed: '{info['title']}' originally by '{original['artist']}' ({original.get('year', 'unknown year')})")
                        
                        # Get file path from track info if available
                        track_data = next((t for t in tracks if t.get('id') == track_id), {})
                        file_path = track_data.get('file_path')
                        
                        pending_cover_updates.append({
                            'track_id': track_id,
                            'title': info['title'],
                            'original_artist': original['artist'],
                            'file_path': file_path,
                            'is_cover_reason': None,
                        })
                        break  # Stop after first matching writer for this track
                    else:
                        logger.debug(f"No original recording found for '{info['title']}' by writer '{writer}'")

        # Step 4: Heuristic fallback when relation/writer metadata is missing or incomplete.
        for track in tracks:
            track_id = track.get('id')
            if not track_id or track_id in seen_track_ids:
                continue

            title = track.get('title', '')
            hint = self._detect_cover_hint_from_text(title, album)
            if not hint:
                continue

            # The heuristic matched a "(X Cover)" pattern in the title or album name.
            # Before treating it as definitive, strip the cover suffix and check
            # whether the credited artist already has an original recording of the
            # base title in the local library.  This prevents falsely marking an
            # artist's own songs as covers when a tribute/cover album uses a
            # title pattern like "Song Title (Artist Cover)".
            _cover_suffix_re = re.compile(r'\s*\([^)]+\s+cover\)\s*$', re.IGNORECASE)
            base_title = _cover_suffix_re.sub('', title).strip()
            if base_title and base_title.lower() != title.lower() and self._artist_has_original(artist, base_title):
                logger.info(
                    f"Skipping heuristic cover detection for '{title}': "
                    f"'{artist}' has an original recording of '{base_title}' in the library"
                )
                continue

            # The hint from the title (e.g. "Within Temptation" in
            # "Radioactive (Within Temptation Cover)") may itself be a cover
            # artist rather than the true original.  If writer metadata is
            # available for this track, attempt a MusicBrainz lookup to resolve
            # the actual original artist before trusting the embedded hint.
            resolved_original_artist = hint
            resolved_original_year = None
            resolved_confidence = 'low'
            resolved_writer = ''
            writers_for_track = (track_writers.get(track_id) or {}).get('writers', [])
            if writers_for_track and base_title:
                for wr in writers_for_track:
                    if self._is_writer_same_as_artist(wr, artist):
                        continue
                    mb_original = self._find_original_recording(base_title, wr, album_artist=artist)
                    if mb_original and not self._names_match(mb_original.get('artist', ''), artist):
                        resolved_original_artist = mb_original['artist']
                        resolved_original_year = mb_original.get('year')
                        resolved_confidence = mb_original.get('confidence', 'medium')
                        resolved_writer = wr
                        logger.info(
                            f"Heuristic hint '{hint}' overridden by writer lookup: "
                            f"'{title}' originally by '{resolved_original_artist}' "
                            f"({resolved_original_year or 'unknown year'})"
                        )
                        break

            result = {
                'track_id': track_id,
                'title': title,
                'is_cover': True,
                'original_artist': resolved_original_artist,
                'original_year': resolved_original_year,
                'writer': resolved_writer,
                'confidence': resolved_confidence
            }
            cover_results.append(result)
            seen_track_ids.add(track_id)

            pending_cover_updates.append({
                'track_id': track_id,
                'title': title,
                'original_artist': resolved_original_artist,
                'file_path': track.get('file_path'),
                'is_cover_reason': f"Heuristic detection: title/album hint ({hint})",
            })

        if pending_cover_updates:
            successful_track_ids = set(self._apply_cover_metadata_batch(pending_cover_updates))
            for update in pending_cover_updates:
                track_id = update['track_id']
                if track_id not in successful_track_ids:
                    continue
                file_path = update.get('file_path')
                if file_path and Path(file_path).exists():
                    new_title = self._build_cover_title(update.get('title', ''), update.get('original_artist'))
                    self._update_file_metadata(file_path, new_title, ["Cover"])
        
        logger.info(f"Cover detection complete: found {len(cover_results)} covers in '{album}'")
        return cover_results

    def _extract_cover_work_ids(self, recording: Dict) -> set:
        """Extract work IDs linked by MB "cover" performance relations from a recording payload."""
        work_ids = set()
        for rel in recording.get('work-relation-list', []) or []:
            if not isinstance(rel, dict):
                continue
            rel_type = str(rel.get('type', '')).strip().lower()
            rel_direction = str(rel.get('direction', '')).strip().lower()
            # In MusicBrainz a cover song has a 'performance' work relation
            # with 'cover' listed in its attributes.  (The type is never
            # literally 'cover' — that was a previous misread of the API.)
            if rel_type != 'performance':
                continue
            attributes = rel.get('attributes') or rel.get('attribute-list') or []
            if not any(str(a).lower() == 'cover' for a in attributes):
                continue
            # "forward" means this recording is a cover of the linked work.
            if rel_direction and rel_direction != 'forward':
                continue
            work_id = (rel.get('work') or {}).get('id')
            if work_id:
                work_ids.add(work_id)
        return work_ids

    def _find_original_from_cover_relation(self, recording_mbid: str, title: str, album_artist: Optional[str] = None) -> Optional[Dict]:
        """Resolve likely original artist/year from MB cover relations on a recording.

        Two detection paths are attempted, most reliable first:

        1. **Direct recording→recording "cover" link** — MusicBrainz editors can
           explicitly tag a recording as "cover recording of" another recording
           (visible on the MB page as "cover recording of: <Title> (<Year>)").
           This is the highest-confidence signal.

        2. **Work-level "performance (cover)" relation** — the recording is linked
           to a work with the "cover" performance attribute.  We then search for
           other recordings linked to the same work to find the original artist.
        """
        fast_path_result = None
        try:
            mb = self._configure_mb_client()

            # Fetch seed recording with both work *and* recording relations.
            seed_result = mb.get_recording_by_id(
                recording_mbid,
                includes=['work-rels', 'recording-rels', 'artist-credits', 'releases']
            )
            seed_recording = seed_result.get('recording', {})

            # ------------------------------------------------------------------
            # Fast path: direct recording→recording "cover" relation.
            # The linked recording IS the original — no further search needed.
            # ------------------------------------------------------------------
            def _artist_from_credit(artist_credit: List[Dict]) -> str:
                for entry in artist_credit or []:
                    if isinstance(entry, dict):
                        name = (entry.get('artist') or {}).get('name')
                        if name:
                            return name
                return ''

            def _year_from_recording(rec: Dict) -> Optional[int]:
                date_str = str(rec.get('first-release-date') or '').strip()
                if len(date_str) >= 4 and date_str[:4].isdigit():
                    try:
                        return int(date_str[:4])
                    except (ValueError, TypeError):
                        pass
                for rel_entry in (rec.get('release-list') or []):
                    if not isinstance(rel_entry, dict):
                        continue
                    rel_date = str(rel_entry.get('date') or '').strip()
                    if len(rel_date) >= 4 and rel_date[:4].isdigit():
                        try:
                            return int(rel_date[:4])
                        except (ValueError, TypeError):
                            pass
                return None

            fast_path_orig_id = None
            for rel in seed_recording.get('recording-relation-list', []) or []:
                if not isinstance(rel, dict):
                    continue
                rel_type = str(rel.get('type', '')).strip().lower()
                direction = str(rel.get('direction', 'forward')).strip().lower()
                # direction="forward" → this recording IS the cover; linked = original
                if rel_type != 'cover' or direction != 'forward':
                    continue

                orig_rec = rel.get('recording') or {}
                orig_id = orig_rec.get('id', '')
                if not orig_id or orig_id == recording_mbid:
                    continue

                orig_artist = _artist_from_credit(orig_rec.get('artist-credit', []))
                orig_year = _year_from_recording(orig_rec)

                # The inline recording object may have limited data; fetch full
                # details when artist or year is missing.
                if not orig_artist or orig_year is None:
                    try:
                        orig_details = mb.get_recording_by_id(
                            orig_id, includes=['artist-credits', 'releases']
                        )
                        orig_full = orig_details.get('recording', {})
                        if not orig_artist:
                            orig_artist = _artist_from_credit(orig_full.get('artist-credit', []))
                        if orig_year is None:
                            orig_year = _year_from_recording(orig_full)
                    except Exception:
                        pass

                if not orig_artist:
                    continue
                if album_artist and self._names_match(orig_artist, album_artist):
                    continue

                logger.debug(
                    "Cover detected via recording→recording link: '%s' originally by '%s' (%s)",
                    title, orig_artist, orig_year or 'unknown year',
                )
                fast_path_result = {
                    'artist': orig_artist,
                    'year': orig_year,
                    'confidence': 'high',
                }
                fast_path_orig_id = orig_id
                break

            # If the fast path found a candidate, verify it is not itself a cover
            # (e.g. BfMV → WT → Imagine Dragons chain).  When the linked recording
            # has a "performance (cover)" work relation, follow the chain: override
            # cover_work_ids with the intermediate's work IDs so the slow path below
            # resolves the true original (the earliest recording of the same work).
            # If the linked recording has no cover work relation it IS the original
            # and we return it immediately.
            cover_work_ids = set()
            if fast_path_result and fast_path_orig_id:
                try:
                    chain_result = mb.get_recording_by_id(
                        fast_path_orig_id, includes=['work-rels']
                    )
                    chain_recording = chain_result.get('recording', {})
                    chained_cover_work_ids = self._extract_cover_work_ids(chain_recording)
                    if chained_cover_work_ids:
                        # The direct "original" is itself a cover; follow the work chain.
                        logger.debug(
                            "Fast-path 'original' '%s' is itself a cover; "
                            "following work chain to find true original",
                            fast_path_result['artist'],
                        )
                        cover_work_ids = chained_cover_work_ids
                        # fast_path_result becomes the fallback if the slow path
                        # finds nothing better.
                    else:
                        # No further cover chain — fast path result is the true original.
                        return fast_path_result
                except Exception:
                    # Unable to inspect the chain; trust the fast path result.
                    return fast_path_result

            # ------------------------------------------------------------------
            # Slow path: work-level "performance (cover)" relation.
            # cover_work_ids may have been set above (chain follow) or comes from
            # the seed recording's own cover performance relation.
            # ------------------------------------------------------------------
            if not cover_work_ids:
                cover_work_ids = self._extract_cover_work_ids(seed_recording)
            if not cover_work_ids:
                return fast_path_result

            search_title = title or seed_recording.get('title') or ''
            if not search_title:
                return None

            result = mb.search_recordings(recording=search_title, limit=50)
            recordings = result.get('recording-list', []) or []

            def _extract_credit_artist_name(artist_credit: List[Dict]) -> str:
                for entry in artist_credit or []:
                    if isinstance(entry, dict):
                        artist_name = (entry.get('artist') or {}).get('name')
                        if artist_name:
                            return artist_name
                return ""

            def _extract_year(full_recording: Dict, release: Optional[Dict] = None) -> Optional[int]:
                candidate_dates = []
                if release:
                    candidate_dates.append(str(release.get('date', '') or ''))
                candidate_dates.append(str(full_recording.get('first-release-date', '') or ''))
                for candidate in candidate_dates:
                    if len(candidate) >= 4 and candidate[:4].isdigit():
                        try:
                            return int(candidate[:4])
                        except (ValueError, TypeError):
                            continue
                return None

            earliest = None
            earliest_year = 9999
            earliest_unknown_year = None

            for recording in recordings:
                recording_id = recording.get('id')
                if not recording_id or recording_id == recording_mbid:
                    continue

                try:
                    details = mb.get_recording_by_id(
                        recording_id,
                        includes=['artist-credits', 'releases', 'work-rels']
                    )
                except Exception:
                    continue

                full_recording = details.get('recording', {})
                recording_work_ids = self._extract_cover_work_ids(full_recording)
                if not recording_work_ids:
                    recording_work_ids = set(
                        (rel.get('work') or {}).get('id')
                        for rel in (full_recording.get('work-relation-list', []) or [])
                        if (rel.get('work') or {}).get('id')
                    )

                if not (recording_work_ids & cover_work_ids):
                    continue

                artist_credit = full_recording.get('artist-credit', []) or recording.get('artist-credit', [])
                recording_artist = _extract_credit_artist_name(artist_credit)
                if not recording_artist:
                    continue

                if album_artist and self._names_match(recording_artist, album_artist):
                    continue

                releases = full_recording.get('release-list', []) or recording.get('release-list', [])
                if not releases:
                    release_year = _extract_year(full_recording)
                    if release_year is not None and release_year < earliest_year:
                        earliest_year = release_year
                        earliest = {
                            'artist': recording_artist,
                            'year': release_year,
                            'confidence': 'high'
                        }
                    elif release_year is None and earliest_unknown_year is None:
                        earliest_unknown_year = {
                            'artist': recording_artist,
                            'year': None,
                            'confidence': 'high'
                        }
                    continue

                for release in releases:
                    release_year = _extract_year(full_recording, release)
                    if release_year is not None and release_year < earliest_year:
                        earliest_year = release_year
                        earliest = {
                            'artist': recording_artist,
                            'year': release_year,
                            'confidence': 'high'
                        }
                    elif release_year is None and earliest_unknown_year is None:
                        earliest_unknown_year = {
                            'artist': recording_artist,
                            'year': None,
                            'confidence': 'high'
                        }

            return earliest or earliest_unknown_year or fast_path_result
        except Exception as e:
            logger.debug(f"Failed MB cover-relation detection for recording '{recording_mbid}': {e}")
            return fast_path_result
    
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
        
        # If no writers from DB, resolve the recording MBID first and then query MusicBrainz.
        if not writers:
            resolved_mbid = track.get('mbid') or self._resolve_recording_mbid(
                track,
                album_artist=track.get('album_artist') or track.get('artist'),
                album_title=track.get('album')
            )
            if resolved_mbid:
                track['mbid'] = resolved_mbid
                writers = self._fetch_writers_from_musicbrainz(resolved_mbid)
        
        return self._normalize_writer_credits(writers)

    def _resolve_recording_mbid(self, track: Dict, album_artist: Optional[str] = None, album_title: Optional[str] = None) -> Optional[str]:
        """Resolve a recording MBID for tracks that only have release-level metadata."""
        existing_mbid = str(track.get('mbid') or '').strip()
        if existing_mbid:
            return existing_mbid

        title = str(track.get('title') or '').strip()
        if not title:
            return None

        search_artist = str(track.get('artist') or album_artist or '').strip()
        release_mbid = str(track.get('musicbrainz_album_mbid') or '').strip()
        mb = self._configure_mb_client()

        target_title = _canonical_track_title(title)

        def _titles_match(left: str, right: str) -> bool:
            left_title = _canonical_track_title(left)
            right_title = _canonical_track_title(right)
            if not left_title or not right_title:
                return False
            return (
                left_title == right_title
                or left_title.startswith(right_title)
                or right_title.startswith(left_title)
            )

        def _artist_matches(artist_credit: List[Dict]) -> bool:
            if not search_artist:
                return True
            for entry in artist_credit or []:
                if not isinstance(entry, dict):
                    continue
                artist_name = (entry.get('artist') or {}).get('name') or entry.get('name')
                if artist_name and self._names_match(artist_name, search_artist):
                    return True
            return False

        if release_mbid:
            try:
                release_result = mb.get_release_by_id(
                    release_mbid,
                    includes=['recordings', 'artist-credits']
                )
                release = release_result.get('release', {})
                for medium in release.get('medium-list', []) or []:
                    for medium_track in medium.get('track-list', []) or []:
                        recording = medium_track.get('recording', {}) or {}
                        candidate_title = recording.get('title') or medium_track.get('title') or ''
                        if not _titles_match(title, candidate_title):
                            continue
                        artist_credit = recording.get('artist-credit', []) or medium_track.get('artist-credit', [])
                        if _artist_matches(artist_credit):
                            resolved_mbid = str(recording.get('id') or '').strip()
                            if resolved_mbid:
                                logger.debug(
                                    f"Resolved recording MBID for '{title}' from album release '{release_mbid}': {resolved_mbid}"
                                )
                                return resolved_mbid
            except Exception as e:
                logger.debug(
                    f"Failed release-based recording MBID lookup for '{title}' on '{album_title or release_mbid}': {e}"
                )

        try:
            search_kwargs = {'recording': title, 'limit': 10}
            if search_artist:
                search_kwargs['artist'] = search_artist
            result = mb.search_recordings(**search_kwargs)
            for recording in result.get('recording-list', []) or []:
                candidate_title = recording.get('title', '')
                if not _titles_match(title, candidate_title):
                    continue
                if not _artist_matches(recording.get('artist-credit', []) or []):
                    continue
                resolved_mbid = str(recording.get('id') or '').strip()
                if resolved_mbid:
                    logger.debug(
                        f"Resolved recording MBID for '{title}' from recording search: {resolved_mbid}"
                    )
                    return resolved_mbid
        except Exception as e:
            logger.debug(f"Failed recording search MBID lookup for '{title}' by '{search_artist}': {e}")

        logger.debug(
            f"Could not resolve recording MBID for '{title}' by '{search_artist or album_artist or 'unknown artist'}'"
        )
        return None
    
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

    def _detect_cover_hint_from_text(self, track_title: str, album_title: str) -> Optional[str]:
        """Infer probable original artist from track or album text."""
        text = f"{track_title or ''} {album_title or ''}".strip()
        if not text:
            return None

        # Examples: "My Song (Metallica Cover)", "originally by Prince"
        patterns = [
            r"\(([^)]+?)\s+cover\)",
            r"originally\s+by\s+([\w\s'&\-.]+)",
            r"cover\s+of\s+([\w\s'&\-.]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                artist_name = match.group(1).strip(" -.,")
                if artist_name:
                    return artist_name

        return None

    def _fetch_writers_from_musicbrainz(self, mbid: str) -> List[str]:
        """
        Fetch writer/composer credits from MusicBrainz recording.
        
        Args:
            mbid: MusicBrainz Recording ID
            
        Returns:
            List of writer names
        """
        try:
            mb = self._configure_mb_client()

            result = mb.get_recording_by_id(
                mbid,
                includes=['artist-rels', 'work-rels']
            )
            
            writers = []
            recording = result.get('recording', {})

            # Direct recording relationships can include writer credits.
            for rel in recording.get('artist-relation-list', []) or []:
                rel_type = str(rel.get('type', '')).lower()
                if rel_type in ('composer', 'lyricist', 'writer'):
                    artist_name = (rel.get('artist') or {}).get('name')
                    if artist_name and artist_name not in writers:
                        writers.append(artist_name)
            
            # Check work relationships for composer/lyricist
            work_rels = recording.get('work-relation-list', [])
            for rel in work_rels:
                work = rel.get('work', {})
                work_id = work.get('id')

                # Get artist relationships from explicit work lookup for reliability.
                artist_rels = work.get('artist-relation-list', [])
                if work_id:
                    try:
                        work_details = mb.get_work_by_id(work_id, includes=['artist-rels'])
                        artist_rels = (work_details.get('work') or {}).get('artist-relation-list', [])
                    except Exception:
                        pass

                for artist_rel in artist_rels:
                    rel_type = str(artist_rel.get('type', '')).lower()
                    if rel_type in ['composer', 'lyricist', 'writer']:
                        artist_name = artist_rel.get('artist', {}).get('name')
                        if artist_name and artist_name not in writers:
                            writers.append(artist_name)
            
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
        normalized = value.lower().strip()
        normalized = normalized.replace("’", "'")
        normalized = re.sub(r"\b(the|and)\b", " ", normalized)
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    def _names_match(self, left: str, right: str) -> bool:
        """Match names with token overlap to handle middle names and variants."""
        left_norm = self._normalize_name(left)
        right_norm = self._normalize_name(right)
        if not left_norm or not right_norm:
            return False
        if left_norm == right_norm:
            return True

        left_tokens = {token for token in left_norm.split() if len(token) > 1}
        right_tokens = {token for token in right_norm.split() if len(token) > 1}
        if not left_tokens or not right_tokens:
            return False

        # If one name's tokens are entirely contained in the other's, treat as a
        # match.  This covers stage-name vs real-name pairs like "Brandy" vs
        # "Brandy Norwood" where the stage name is a single token that is a subset
        # of the full legal name's tokens.
        if left_tokens <= right_tokens or right_tokens <= left_tokens:
            return True

        intersection = left_tokens & right_tokens
        min_required = min(len(left_tokens), len(right_tokens))
        return len(intersection) >= max(2, min_required)

    def _artist_has_original(self, artist: str, title: str) -> bool:
        """Return True if the artist already has a non-cover recording of *title*
        in the local library database.

        Used as a pre-flight guard before heuristic or writer-based cover detection
        to avoid falsely flagging an artist's own songs as covers when a tribute /
        cover album happens to name a track like "Song Title (X Cover)".
        """
        if not self.db_conn or not artist or not title:
            return False
        try:
            cursor = self.db_conn.cursor()
            # self.placeholder is a fixed constant ("%s") and not user-supplied input,
            # so there is no SQL-injection risk from its use in the f-string.
            cursor.execute(
                f"""
                SELECT 1 FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER({self.placeholder})
                  AND LOWER(title) = LOWER({self.placeholder})
                                    AND COALESCE(is_cover, 0) = 0
                LIMIT 1
                """,
                (artist, title),
            )
            return cursor.fetchone() is not None
        except Exception as exc:
            logger.debug(
                f"[CoverDetector] _artist_has_original lookup failed for "
                f"{artist!r}/{title!r}: {exc}"
            )
            return False

    def _is_common_writer_for_artist(self, writer: str, artist: str, track_artist: Optional[str] = None, min_count: int = 2) -> bool:
        """Return True if *writer* appears on at least *min_count* tracks by the
        relevant artist in the local library, indicating they are a regular
        collaborator or producer rather than a one-off songwriter whose credit
        signals a cover.

        On compilation albums the ``album_artist`` is typically "Various Artists"
        and is not a useful lookup key; *track_artist* (the per-track credited
        artist) is used instead when it differs from *artist*.

        Writer credits are stored as a JSON array in ``tracks.writer``.  The
        comparison is performed in Python (after fetching all non-null writer rows)
        so that the same normalisation logic used elsewhere is applied — avoiding
        false negatives from capitalisation or punctuation differences.
        """
        if not self.db_conn or not writer or not artist:
            return False

        # On compilations, prefer the per-track artist for the DB lookup so
        # that "Various Artists" is never used as the lookup key.
        lookup_artist = (
            track_artist
            if track_artist and not self._names_match(track_artist, artist)
            else artist
        )

        try:
            cursor = self.db_conn.cursor()
            # self.placeholder is a fixed constant ("%s"), not user input.
            # Query by both album-artist path and raw track artist so that tracks
            # on compilation albums (where album_artist = "Various Artists" but
            # artist = the band name) are found correctly.
            cursor.execute(
                f"""
                SELECT writer FROM tracks
                WHERE (
                    LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER({self.placeholder})
                    OR LOWER(artist) = LOWER({self.placeholder})
                )
                  AND writer IS NOT NULL
                  AND writer != ''
                  AND writer != '[]'
                """,
                (lookup_artist, lookup_artist),
            )
            rows = cursor.fetchall() or []
        except Exception as exc:
            logger.debug(
                f"[CoverDetector] _is_common_writer_for_artist query failed for "
                f"{artist!r}/{writer!r}: {exc}"
            )
            return False

        writer_norm = self._normalize_name(writer)
        count = 0
        for row in rows:
            raw = self._row_value(row, 'writer', index=0, default='')
            if not raw:
                continue
            try:
                names = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(names, list):
                    names = [str(names)]
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            # Count each track once regardless of how many times the writer
            # appears in that track's credit list.
            if any(self._normalize_name(str(n)) == writer_norm for n in names):
                count += 1
                if count >= min_count:
                    logger.debug(
                        f"[CoverDetector] '{writer}' is a common writer for '{lookup_artist}' "
                        f"({count}/{min_count} tracks) — not a cover indicator"
                    )
                    return True
        return False

    def _find_original_recording(self, title: str, writer: str, album_artist: Optional[str] = None) -> Optional[Dict]:
        """Find earliest likely original recording for a title/writer pair."""
        try:
            mb = self._configure_mb_client()

            # First query works by title+writer. This mirrors the MusicBrainz web
            # Works view and gives us canonical writer-linked work IDs.
            matched_work_ids = set()
            try:
                work_result = mb.search_works(work=title, artist=writer, limit=25)
            except Exception:
                work_result = {}

            for work in work_result.get('work-list', []) or []:
                work_id = work.get('id')
                if not work_id:
                    continue
                work_title = work.get('title', '')
                if work_title and self._normalize_name(work_title) != self._normalize_name(title):
                    # Keep this strict to avoid attaching unrelated writer works.
                    continue
                matched_work_ids.add(work_id)

            result = mb.search_recordings(recording=title, limit=25)
            recordings = result.get('recording-list', [])
            if not recordings:
                return None

            def _canonical_title(value: str) -> str:
                # Remove common version suffixes like "(demo)", "[live]", "- remaster" so
                # original recordings still match album track variants.
                text = str(value or "").strip()
                text = re.sub(r"\[[^\]]*\]", " ", text)
                text = re.sub(r"\([^)]*\)", " ", text)
                text = re.sub(r"\s+-\s+.*$", " ", text)
                return self._normalize_name(text)

            def _titles_match(left: str, right: str) -> bool:
                left_raw = self._normalize_name(left)
                right_raw = self._normalize_name(right)
                left_can = _canonical_title(left)
                right_can = _canonical_title(right)

                if not left_raw or not right_raw:
                    return False

                # Exact raw match first, then canonicalized match.
                if left_raw == right_raw or (left_can and left_can == right_can):
                    return True

                # Allow minor suffix/prefix variants once canonicalized.
                if left_can and right_can:
                    return left_can.startswith(right_can) or right_can.startswith(left_can)

                return False

            def _extract_credit_artist_name(artist_credit: List[Dict]) -> str:
                # MusicBrainz may return a mixed list of dict/joinphrase tokens.
                for entry in artist_credit or []:
                    if isinstance(entry, dict):
                        artist_name = (entry.get('artist') or {}).get('name')
                        if artist_name:
                            return artist_name
                return ""

            title_norm = self._normalize_name(title)
            earliest = None
            earliest_year = 9999
            earliest_unknown_year = None
            fallback_earliest = None
            fallback_earliest_year = 9999
            fallback_unknown_year = None

            def _extract_year(full_recording: Dict, release: Optional[Dict] = None) -> Optional[int]:
                """Extract a usable year from release dates or first-release-date."""
                candidate_dates = []
                if release:
                    candidate_dates.append(str(release.get('date', '') or ''))
                candidate_dates.append(str(full_recording.get('first-release-date', '') or ''))
                for candidate in candidate_dates:
                    if len(candidate) >= 4 and candidate[:4].isdigit():
                        try:
                            return int(candidate[:4])
                        except (ValueError, TypeError):
                            continue
                return None

            for recording in recordings:
                recording_id = recording.get('id')
                if not recording_id:
                    continue

                try:
                    details = mb.get_recording_by_id(
                        recording_id,
                        includes=['artist-credits', 'releases', 'work-rels', 'artist-rels']
                    )
                except Exception:
                    continue

                full_recording = details.get('recording', {})
                recording_title = full_recording.get('title') or recording.get('title') or ''
                if title_norm and not _titles_match(title, recording_title):
                    continue

                writer_names = []

                for rel in full_recording.get('artist-relation-list', []) or []:
                    rel_type = str(rel.get('type', '')).lower()
                    if rel_type in ('composer', 'writer', 'lyricist'):
                        artist_name = (rel.get('artist') or {}).get('name')
                        if artist_name:
                            writer_names.append(artist_name)

                recording_work_ids = set()

                # Resolve work-level writers via explicit work lookup.
                for rel in full_recording.get('work-relation-list', []) or []:
                    work_id = (rel.get('work') or {}).get('id')
                    if not work_id:
                        continue
                    recording_work_ids.add(work_id)
                    try:
                        work_details = mb.get_work_by_id(work_id, includes=['artist-rels'])
                        work_data = work_details.get('work', {})
                        for work_rel in work_data.get('artist-relation-list', []) or []:
                            rel_type = str(work_rel.get('type', '')).lower()
                            if rel_type in ('composer', 'writer', 'lyricist'):
                                artist_name = (work_rel.get('artist') or {}).get('name')
                                if artist_name:
                                    writer_names.append(artist_name)
                    except Exception:
                        pass

                writer_names = self._normalize_writer_credits(writer_names)
                writer_match = any(self._names_match(writer, candidate) for candidate in writer_names)

                # Strong signal: recording links to a work returned by title+writer search.
                if not writer_match and matched_work_ids and (recording_work_ids & matched_work_ids):
                    writer_match = True

                artist_credit = full_recording.get('artist-credit', []) or recording.get('artist-credit', [])
                if not artist_credit:
                    continue
                recording_artist = _extract_credit_artist_name(artist_credit)
                if not recording_artist:
                    continue

                artist_is_different = True
                if album_artist:
                    artist_is_different = not self._names_match(recording_artist, album_artist)

                if not writer_match and not artist_is_different:
                    continue

                releases = full_recording.get('release-list', []) or recording.get('release-list', [])
                if not releases:
                    release_year = _extract_year(full_recording)
                    if writer_match:
                        if release_year is not None:
                            if release_year < earliest_year:
                                earliest_year = release_year
                                earliest = {
                                    'artist': recording_artist,
                                    'year': release_year,
                                    'confidence': 'high' if writer_names else 'medium'
                                }
                        elif earliest_unknown_year is None:
                            earliest_unknown_year = {
                                'artist': recording_artist,
                                'year': None,
                                'confidence': 'medium'
                            }
                    elif artist_is_different:
                        if release_year is not None:
                            if release_year < fallback_earliest_year:
                                fallback_earliest_year = release_year
                                fallback_earliest = {
                                    'artist': recording_artist,
                                    'year': release_year,
                                    'confidence': 'low'
                                }
                        elif fallback_unknown_year is None:
                            fallback_unknown_year = {
                                'artist': recording_artist,
                                'year': None,
                                'confidence': 'low'
                            }
                    continue

                for release in releases:
                    year = _extract_year(full_recording, release)

                    if writer_match:
                        if year is not None and year < earliest_year:
                            earliest_year = year
                            earliest = {
                                'artist': recording_artist,
                                'year': year,
                                'confidence': 'high' if writer_names else 'medium'
                            }
                        elif year is None and earliest_unknown_year is None:
                            earliest_unknown_year = {
                                'artist': recording_artist,
                                'year': None,
                                'confidence': 'medium'
                            }
                    elif artist_is_different:
                        if year is not None and year < fallback_earliest_year:
                            fallback_earliest_year = year
                            fallback_earliest = {
                                'artist': recording_artist,
                                'year': year,
                                'confidence': 'low'
                            }
                        elif year is None and fallback_unknown_year is None:
                            fallback_unknown_year = {
                                'artist': recording_artist,
                                'year': None,
                                'confidence': 'low'
                            }

            return earliest or earliest_unknown_year or fallback_earliest or fallback_unknown_year

        except Exception as e:
            logger.debug(f"Failed to find original recording for '{title}' by '{writer}': {e}")
            return None

    def update_cover_metadata(self, track_id: str, title: str, original_artist: Optional[str],
                            file_path: Optional[str] = None, is_cover_reason: Optional[str] = None) -> bool:
        """Update track metadata to reflect cover attribution."""
        try:
            new_title = self._build_cover_title(title, original_artist)
            successful_track_ids = self._apply_cover_metadata_batch([
                {
                    'track_id': track_id,
                    'title': title,
                    'original_artist': original_artist,
                    'file_path': file_path,
                    'is_cover_reason': is_cover_reason,
                }
            ])
            if track_id not in successful_track_ids:
                return False

            logger.info(f"✓ Database updated: '{title}' → '{new_title}' (original: {original_artist})")

            if file_path and Path(file_path).exists():
                self._update_file_metadata(file_path, new_title, ["Cover"])

            return True

        except Exception as e:
            if self.db_conn:
                try:
                    self.db_conn.rollback()
                except Exception:
                    pass
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
