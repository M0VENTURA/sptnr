#!/usr/bin/env python3
"""
Cover song detection module for automatic identification and attribution.

Detects cover songs by analyzing songwriter/composer data from MusicBrainz,
then attributes the original artist and updates track metadata accordingly.
"""

import logging
import json
import re
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from api_clients.musicbrainz import _VERSION as MUSICBRAINZ_VERSION

logger = logging.getLogger(__name__)


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
        self.placeholder = "%s" if self.is_pg else "?"
        self._band_members_cache = {}  # Cache to avoid repeated API calls

    def _configure_musicbrainzngs(self):
        """Ensure musicbrainzngs identifies itself with the app user agent."""
        try:
            import musicbrainzngs as mb
            mb.set_useragent("sptnr", MUSICBRAINZ_VERSION, "https://github.com/M0VENTURA/sptnr")
            return mb
        except Exception as e:
            logger.debug(f"Failed to configure musicbrainzngs user agent: {e}")
            return None
    
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
            for writer in info['writers']:
                # Check if writer is different from album artist
                if not self._is_writer_same_as_artist(writer, artist):
                    logger.info(f"Potential cover: '{info['title']}' - lyricist/writer '{writer}' differs from artist '{artist}'")
                    
                    # Look up original recording by this writer
                    original = self._find_original_recording(
                        info['title'],
                        writer,
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
                            'writer': writer,
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
                        break  # Stop after first matching writer for this track
                    else:
                        logger.debug(f"No original recording found for '{info['title']}' by writer '{writer}'")
        
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
        """
        Fetch writer/composer credits from MusicBrainz recording.
        
        Args:
            mbid: MusicBrainz Recording ID
            
        Returns:
            List of writer names
        """
        try:
            mb = self._configure_musicbrainzngs()
            if mb is None:
                return []
            
            result = mb.get_recording_by_id(
                mbid,
                includes=['artist-rels', 'work-rels']
            )
            
            writers = []
            recording = result.get('recording', {})
            
            # Check work relationships for composer/lyricist
            work_rels = recording.get('work-relation-list', [])
            for rel in work_rels:
                work = rel.get('work', {})
                # Get artist relationships from the work
                artist_rels = work.get('artist-relation-list', [])
                for artist_rel in artist_rels:
                    rel_type = artist_rel.get('type', '')
                    if rel_type in ['composer', 'lyricist', 'writer']:
                        artist_name = artist_rel.get('artist', {}).get('name')
                        if artist_name and artist_name not in writers:
                            writers.append(artist_name)

                # Some recording lookups return only work IDs without embedded artist
                # relations, so resolve work details explicitly as a fallback.
                if not artist_rels:
                    work_id = work.get('id')
                    if work_id:
                        try:
                            work_details = mb.get_work_by_id(work_id, includes=['artist-rels'])
                            work_data = work_details.get('work', {})
                            for work_rel in work_data.get('artist-relation-list', []) or []:
                                rel_type = str(work_rel.get('type', '')).lower()
                                if rel_type in ['composer', 'lyricist', 'writer']:
                                    artist_name = (work_rel.get('artist') or {}).get('name')
                                    if artist_name and artist_name not in writers:
                                        writers.append(artist_name)
                        except Exception:
                            pass
            
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
        writer: str,
        album_artist: Optional[str] = None,
        recording_mbid: Optional[str] = None
    ) -> Optional[Dict]:
        """Find earliest likely original recording for a title/writer pair."""
        try:
            mb = self._configure_musicbrainzngs()
            if mb is None:
                return None

            target_work_ids = set()
            if recording_mbid:
                try:
                    target_result = mb.get_recording_by_id(recording_mbid, includes=['work-rels'])
                    target_recording = target_result.get('recording', {})
                    for rel in target_recording.get('work-relation-list', []) or []:
                        work_id = (rel.get('work') or {}).get('id')
                        if work_id:
                            target_work_ids.add(work_id)
                except Exception:
                    pass

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

            recordings_by_id: Dict[str, Dict] = {}

            # Title search remains useful as a broad candidate source.
            result = mb.search_recordings(recording=title, limit=25)
            for rec in result.get('recording-list', []) or []:
                rec_id = rec.get('id')
                if rec_id:
                    recordings_by_id[rec_id] = rec

            # Work-linked expansion prevents wrong matches for common song titles.
            # Prefer target work ids (from scanned MBID). If missing, use top matched
            # work ids from title+writer search.
            work_ids_for_expansion = set(target_work_ids)
            if not work_ids_for_expansion and matched_work_ids:
                work_ids_for_expansion.update(list(matched_work_ids)[:10])

            for work_id in work_ids_for_expansion:
                try:
                    work_details = mb.get_work_by_id(work_id, includes=['recording-rels'])
                    work_data = work_details.get('work', {})
                    for rel in work_data.get('recording-relation-list', []) or []:
                        rec = rel.get('recording') or {}
                        rec_id = rec.get('id')
                        if rec_id and rec_id not in recordings_by_id:
                            recordings_by_id[rec_id] = {
                                'id': rec_id,
                                'title': rec.get('title', ''),
                            }
                except Exception:
                    continue

            recordings = list(recordings_by_id.values())
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

            def _looks_non_original_release(release_title: str) -> bool:
                text = self._normalize_name(release_title or "")
                if not text:
                    return False
                blocked_markers = (
                    'live', 'karaoke', 'tribute', 'remix', 'instrumental',
                    'greatest hits', 'best of', 'compilation'
                )
                return any(marker in text for marker in blocked_markers)

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

                # Highest-confidence path: same underlying work as the scanned track MBID.
                same_work_match = bool(target_work_ids and (recording_work_ids & target_work_ids))
                matched_work_match = bool(matched_work_ids and (recording_work_ids & matched_work_ids))

                # For common titles, require either title affinity or work linkage.
                title_matches = (not title_norm) or _titles_match(title, recording_title)
                if not title_matches and not same_work_match and not matched_work_match:
                    continue

                # If the scanned track has known work IDs, prefer same-work recordings,
                # but do NOT hard-skip recordings with both title and writer matches.
                # (Cake's cover of "I Will Survive" may link to a different work node
                #  than Gloria Gaynor's original, but title+writer still confirm it.)
                if target_work_ids and not same_work_match:
                    if not (title_matches and writer_match):
                        continue

                # If we have writer-matched work IDs, reject recordings linked to different works.
                if matched_work_ids and not matched_work_match and not writer_match:
                    continue

                # Strong signal: recording links to a work returned by title+writer search.
                if not writer_match and matched_work_match:
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

                if not writer_match and not artist_is_different and not same_work_match:
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
                    release_title = str(release.get('title', '') or '')
                    is_official = str(release.get('status', '') or '').lower() == 'official'
                    is_likely_non_original = _looks_non_original_release(release_title)

                    if same_work_match or writer_match:
                        # Prefer official and non-compilation/live releases for original attribution.
                        confidence = 'high' if (same_work_match or writer_names) else 'medium'
                        if is_likely_non_original:
                            confidence = 'medium' if confidence == 'high' else confidence

                        if year is not None and year < earliest_year:
                            earliest_year = year
                            earliest = {
                                'artist': recording_artist,
                                'year': year,
                                'confidence': confidence,
                                'official': is_official,
                            }
                        elif year is None and earliest_unknown_year is None:
                            earliest_unknown_year = {
                                'artist': recording_artist,
                                'year': None,
                                'confidence': 'medium',
                                'official': is_official,
                            }
                    elif artist_is_different:
                        if year is not None and year < fallback_earliest_year:
                            fallback_earliest_year = year
                            fallback_earliest = {
                                'artist': recording_artist,
                                'year': year,
                                'confidence': 'low',
                                'official': is_official,
                            }
                        elif year is None and fallback_unknown_year is None:
                            fallback_unknown_year = {
                                'artist': recording_artist,
                                'year': None,
                                'confidence': 'low',
                                'official': is_official,
                            }

            return earliest or earliest_unknown_year or fallback_earliest or fallback_unknown_year

        except Exception as e:
            logger.debug(f"Failed to find original recording for '{title}' by '{writer}': {e}")
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
                    current_genres = (result['genres'] if self.is_pg else result[0]) or ""
                    genres_list = [g.strip() for g in current_genres.split(",")] if current_genres else []
                    if "Cover" not in genres_list:
                        genres_list.append("Cover")
                    new_genres = ", ".join(genres_list)
                    cursor.execute(
                        f"UPDATE tracks SET genres = {self.placeholder} WHERE id = {self.placeholder}",
                        (new_genres, track_id)
                    )

                is_cover_value = True if self.is_pg else 1
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
