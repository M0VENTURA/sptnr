#!/usr/bin/env python3
"""
Cover song detection module for automatic identification and attribution.

Detects cover songs by analyzing songwriter/composer data from MusicBrainz,
then attributes the original artist and updates track metadata accordingly.

Uses the existing ``MusicBrainzHttpClient`` from ``api_clients.musicbrainz_http``
rather than building its own HTTP client.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from api_clients.musicbrainz_http import MusicBrainzHttpClient
from db.utils import get_db_connection, row_get

logger = logging.getLogger(__name__)

# Matches a trailing "(X Cover)" annotation in a track title.
_COVER_SUFFIX_RE = re.compile(r'\s*\([^)]+\s+cover\)\s*$', re.IGNORECASE)

# Compilation artist names used to decide when per-track-artist lookups are needed.
_COMPILATION_ARTIST_NAMES = frozenset({
    'various artists', 'various', 'v/a', 'va', 'compilation', 'soundtrack',
})


# ---------------------------------------------------------------------------
# Title normalisation helpers
# ---------------------------------------------------------------------------

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


def _normalize_name(value: str) -> str:
    """Normalize person/group names for robust matching."""
    if not value:
        return ""
    normalized = value.lower().strip()
    normalized = normalized.replace("'", "'")
    normalized = re.sub(r"\b(the|and)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _names_match(left: str, right: str) -> bool:
    """Match names with token overlap to handle middle names and variants."""
    left_norm = _normalize_name(left)
    right_norm = _normalize_name(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    left_tokens = {t for t in left_norm.split() if len(t) > 1}
    right_tokens = {t for t in right_norm.split() if len(t) > 1}
    if not left_tokens or not right_tokens:
        return False
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return True
    intersection = left_tokens & right_tokens
    return len(intersection) >= max(2, min(len(left_tokens), len(right_tokens)))


# ---------------------------------------------------------------------------
# Helpers for extracting data from MB JSON-API responses
# ---------------------------------------------------------------------------

def _artist_from_credit(artist_credit: List[Dict]) -> str:
    for entry in artist_credit or []:
        if isinstance(entry, dict):
            name = (entry.get("artist") or {}).get("name")
            if name:
                return name
    return ""


def _year_from_recording(rec: Dict) -> Optional[int]:
    date_str = str(rec.get("first-release-date") or "").strip()
    if len(date_str) >= 4 and date_str[:4].isdigit():
        try:
            return int(date_str[:4])
        except (ValueError, TypeError):
            pass
    for rel in rec.get("release-list") or []:
        if isinstance(rel, dict):
            rel_date = str(rel.get("date") or "").strip()
            if len(rel_date) >= 4 and rel_date[:4].isdigit():
                try:
                    return int(rel_date[:4])
                except (ValueError, TypeError):
                    pass
    return None


def _extract_work_ids(recording: Dict) -> Set[str]:
    """Extract all work IDs from a recording's work-relation-list."""
    ids: Set[str] = set()
    for rel in recording.get("work-relation-list", []) or []:
        if not isinstance(rel, dict):
            continue
        wid = (rel.get("work") or {}).get("id")
        if wid:
            ids.add(wid)
    return ids


def _extract_cover_work_ids(recording: Dict) -> Set[str]:
    """Extract work IDs linked by MB 'performance (cover)' relations."""
    ids: Set[str] = set()
    for rel in recording.get("work-relation-list", []) or []:
        if not isinstance(rel, dict):
            continue
        if str(rel.get("type", "")).strip().lower() != "performance":
            continue
        attrs = rel.get("attributes") or rel.get("attribute-list") or []
        if not any(str(a).lower() == "cover" for a in attrs):
            continue
        direction = str(rel.get("direction", "")).strip().lower()
        if direction and direction != "forward":
            continue
        wid = (rel.get("work") or {}).get("id")
        if wid:
            ids.add(wid)
    return ids


def _normalize_writer_credits(writers: List[str]) -> List[str]:
    """Split combined writer credits and dedupe names."""
    normalized: List[str] = []
    for writer in writers or []:
        text = str(writer or "").strip()
        if not text:
            continue
        parts = re.split(r"\s*[;/,&]|\s+and\s+", text, flags=re.IGNORECASE)
        for part in parts:
            name = re.sub(r"^\(+|\)+$", "", part.strip())
            if name and name not in normalized:
                normalized.append(name)
    return normalized


# ---------------------------------------------------------------------------
# CoverDetector
# ---------------------------------------------------------------------------

class CoverDetector:
    """Detect and attribute cover songs using MusicBrainz relations and writer/composer data.

    Detection stages (in order of confidence):

    1. **Direct recording→recording "cover" relation** — MusicBrainz editors
       explicitly tag a recording as "cover recording of" another recording.
       (confidence: **high**)

    2. **Work-level "performance (cover)" relation** — recording linked to a
       work via a cover-performance attribute; searches for the earliest
       non-cover recording of that work. (confidence: **high**)

    3. **Writer/composer mismatch** — writer differs from album artist;
       looks up the earliest recording of the title by that writer.
       (confidence: **medium**)

    4. **ISRC-based matching** — when the track has an ISRC code, looks up
       the original recording via ISRC for the highest-accuracy match.
       (confidence: **high**)

    5. **Heuristic title annotation** — title contains ``(X Cover)`` or
       ``originally by X``. (confidence: **low**)

    6. **Work-based fallback** — for already-flagged covers where the
       original artist is still unknown, resolves via work-linked recordings.
       (confidence: **low**)
    """

    def __init__(self, db_connection=None):
        self.mb = MusicBrainzHttpClient()
        self.db_conn = db_connection
        self._band_members_cache: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def detect_covers_for_album(
        self,
        album: str,
        artist: str,
        tracks: List[Dict],
    ) -> List[Dict]:
        """Detect cover songs in an album.

        Returns a list of result dicts:
            {track_id, title, is_cover, original_artist, original_year,
             writer, confidence}
        """
        logger.info("Starting cover detection for album '%s' by '%s' (%d tracks)",
                     album, artist, len(tracks))

        for track in tracks:
            track.setdefault("album", album)
            track.setdefault("album_artist", artist)

        seen_track_ids: Set[str] = set()
        cover_results: List[Dict] = []
        pending_updates: List[Dict] = []

        # Per-album release cache — avoids re-fetching the same release for
        # every track when doing release-level fallback lookups.
        _release_cache: Dict[str, dict] = {}

        # Step 0: skip tracks already fully confirmed.
        for track in tracks:
            tid = track.get("id")
            if not tid:
                continue
            if self._is_already_confirmed_cover(track):
                seen_track_ids.add(tid)

        # Step 1: collect writer info for all tracks.
        track_writers = self._collect_track_writers(tracks, artist)

        # Step 2: ISRC-based matching (fastest, highest confidence).
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            isrc = str(track.get("isrc") or "").strip()
            if not isrc:
                continue
            result = self._detect_via_isrc(
                isrc=isrc,
                track_id=tid,
                title=track.get("title", ""),
                album_artist=artist,
            )
            if result:
                cover_results.append(result)
                seen_track_ids.add(tid)
                pending_updates.append(self._build_update(result, track))

        # Step 3: MusicBrainz recording-relation detection (cover recording of).
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            mbid = self._resolve_recording_mbid(track, artist, album)
            if not mbid:
                continue
            track["mbid"] = mbid

            result = self._detect_via_recording_relation(
                recording_mbid=mbid,
                track_id=tid,
                title=track.get("title", ""),
                album_artist=artist,
                release_mbid=str(track.get("musicbrainz_album_mbid") or "").strip(),
                _release_cache=_release_cache,
            )
            if result:
                cover_results.append(result)
                seen_track_ids.add(tid)
                pending_updates.append(self._build_update(result, track))

        # Step 4: writer-based detection (medium confidence).
        is_compilation = artist.lower() in _COMPILATION_ARTIST_NAMES

        # Pre-check writer coverage for non-compilations.
        writer_skip_all = False
        if not is_compilation and track_writers:
            coverage = self._writer_coverage_for_artist(artist)
            if coverage < 0.10:
                logger.info("Writer coverage for '%s' is %.1f%% (<10%%) — skipping writer-based detection",
                            artist, coverage * 100)
                writer_skip_all = True

        track_artist_coverage_cache: Dict[str, float] = {}

        for tid, info in track_writers.items():
            if tid in seen_track_ids:
                continue
            if writer_skip_all:
                break

            if is_compilation:
                ta = info.get("track_artist", "") or ""
                if ta not in track_artist_coverage_cache:
                    track_artist_coverage_cache[ta] = self._writer_coverage_for_artist(ta)
                if track_artist_coverage_cache[ta] < 0.10:
                    continue

            for writer in info.get("writers", []):
                if self._is_writer_same_as_artist(writer, artist):
                    continue
                if self._artist_has_original(artist, info["title"]):
                    break
                if self._is_common_writer_for_artist(
                    writer, artist, track_artist=info.get("track_artist")
                ):
                    break

                search_title = _COVER_SUFFIX_RE.sub("", info["title"]).strip() or info["title"]
                original = self._find_original_recording(search_title, writer, album_artist=artist)
                if not original:
                    continue
                if _names_match(original.get("artist", ""), artist):
                    break

                result = {
                    "track_id": tid,
                    "title": info["title"],
                    "is_cover": True,
                    "original_artist": original["artist"],
                    "original_year": original.get("year"),
                    "writer": writer,
                    "confidence": original.get("confidence", "medium"),
                }
                cover_results.append(result)
                seen_track_ids.add(tid)
                pending_updates.append(self._build_update(result, info))
                break

        # Step 5: heuristic title/text-based detection.
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            title = track.get("title", "")
            hint = self._detect_cover_hint_from_text(title, album)
            if not hint:
                continue

            # Avoid false positive when the artist already owns the original.
            base_title = _COVER_SUFFIX_RE.sub("", title).strip()
            if base_title and base_title.lower() != title.lower() and self._artist_has_original(artist, base_title):
                continue

            # Try to resolve the hint via writer lookup for higher confidence.
            resolved_artist = hint
            resolved_year: Optional[int] = None
            resolved_confidence = "low"
            resolved_writer = ""
            writers_for_track = (track_writers.get(tid) or {}).get("writers", [])
            if writers_for_track and base_title:
                for wr in writers_for_track:
                    if self._is_writer_same_as_artist(wr, artist):
                        continue
                    mb_orig = self._find_original_recording(base_title, wr, album_artist=artist)
                    if mb_orig and not _names_match(mb_orig.get("artist", ""), artist):
                        resolved_artist = mb_orig["artist"]
                        resolved_year = mb_orig.get("year")
                        resolved_confidence = mb_orig.get("confidence", "medium")
                        resolved_writer = wr
                        break

            result = {
                "track_id": tid,
                "title": title,
                "is_cover": True,
                "original_artist": resolved_artist,
                "original_year": resolved_year,
                "writer": resolved_writer,
                "confidence": resolved_confidence,
            }
            cover_results.append(result)
            seen_track_ids.add(tid)
            pending_updates.append(self._build_update(result, track))

        # Step 6: work-based fallback for already-flagged covers.
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            if not self._is_cover_flagged(track):
                continue
            if (track.get("original_cover_artist") or "").strip():
                continue

            mbid = self._resolve_recording_mbid(track, artist, album)
            if not mbid:
                continue
            track["mbid"] = mbid
            original = self._find_original_via_work_lookup(mbid, track.get("title", ""), artist)
            if not original:
                continue

            result = {
                "track_id": tid,
                "title": track.get("title", ""),
                "is_cover": True,
                "original_artist": original["artist"],
                "original_year": original.get("year"),
                "writer": "",
                "confidence": original.get("confidence", "low"),
            }
            cover_results.append(result)
            seen_track_ids.add(tid)
            pending_updates.append(self._build_update(result, track))

        # Persist updates.
        if pending_updates:
            successful = set(self._apply_cover_metadata_batch(pending_updates))
            for update in pending_updates:
                tid = update.get("track_id")
                if tid not in successful:
                    continue
                fp = update.get("file_path")
                if fp and Path(fp).exists():
                    new_title = self._build_cover_title(
                        update.get("title", ""), update.get("original_artist")
                    )
                    self._update_file_metadata(fp, new_title, ["Cover"])

        logger.info("Cover detection complete: %d covers found in '%s'", len(cover_results), album)
        return cover_results

    # ------------------------------------------------------------------
    # Detection methods
    # ------------------------------------------------------------------

    def is_cover(self, title: str, artist: str, composer: Optional[str] = None,
                 writer: Optional[str] = None) -> bool:
        """Quick single-track cover check used by the legacy scan hook."""
        if not title:
            return False
        if _COVER_SUFFIX_RE.search(title):
            return True
        if "cover" in title.lower():
            return True
        if composer or writer:
            candidates = [c for c in [composer, writer] if c]
            for c in candidates:
                if not _names_match(c, artist):
                    return True
        return False

    def _detect_via_isrc(self, isrc: str, track_id: str, title: str,
                         album_artist: str) -> Optional[Dict]:
        """ISRC-based detection — highest confidence when ISRC is available."""
        try:
            recordings = self.mb.lookup_by_isrc(isrc, inc="artist-credits+releases+work-rels+recording-rels")
            if not recordings:
                return None

            # The ISRC lookup returns recordings; the earliest release is the original.
            earliest: Optional[Dict] = None
            earliest_year = 9999

            for rec in recordings:
                rec_id = rec.get("id")
                if not rec_id:
                    continue
                rec_artist = _artist_from_credit(rec.get("artist-credit", []))
                if not rec_artist:
                    continue
                if _names_match(rec_artist, album_artist):
                    continue
                year = _year_from_recording(rec)
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "high"}
                elif year is None and earliest is None:
                    earliest = {"artist": rec_artist, "year": None, "confidence": "high"}

            if earliest:
                logger.info("ISRC-based cover: '%s' originally by '%s'", title, earliest["artist"])
            return earliest
        except Exception as exc:
            logger.debug("ISRC lookup failed for %s: %s", isrc, exc)
            return None

    def _detect_via_recording_relation(
        self,
        recording_mbid: str,
        track_id: str,
        title: str,
        album_artist: str,
        release_mbid: str = "",
        _release_cache: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Detect cover via MusicBrainz recording→recording 'cover' relations."""
        fast_result = None
        fast_orig_id = None

        try:
            seed = self.mb.get_recording(
                recording_mbid,
                inc="work-rels+recording-rels+artist-credits+releases",
            )
            if not seed:
                return None

            # Fast path: direct recording→recording "cover" link.
            for rel in seed.get("recording-relation-list", []) or []:
                if not isinstance(rel, dict):
                    continue
                rt = str(rel.get("type", "")).strip().lower()
                direction = str(rel.get("direction", "forward")).strip().lower()
                if rt != "cover" or direction != "forward":
                    continue
                orig_rec = rel.get("recording") or {}
                orig_id = orig_rec.get("id", "")
                if not orig_id or orig_id == recording_mbid:
                    continue
                orig_artist = _artist_from_credit(orig_rec.get("artist-credit", []))
                orig_year = _year_from_recording(orig_rec)
                if not orig_artist or orig_year is None:
                    try:
                        details = self.mb.get_recording(orig_id, inc="artist-credits+releases")
                        if not orig_artist:
                            orig_artist = _artist_from_credit(details.get("artist-credit", []))
                        if orig_year is None:
                            orig_year = _year_from_recording(details)
                    except Exception:
                        pass
                if not orig_artist:
                    continue
                if album_artist and _names_match(orig_artist, album_artist):
                    continue
                logger.debug("Cover via recording relation: '%s' originally by '%s'",
                             title, orig_artist)
                fast_result = {"artist": orig_artist, "year": orig_year, "confidence": "high"}
                fast_orig_id = orig_id
                break

            # If fast path found a candidate, check it's not itself a cover.
            cover_work_ids: Set[str] = set()
            if fast_result and fast_orig_id:
                try:
                    chain = self.mb.get_recording(fast_orig_id, inc="work-rels")
                    chained_cover = _extract_cover_work_ids(chain)
                    if chained_cover:
                        cover_work_ids = chained_cover
                    else:
                        return fast_result
                except Exception:
                    return fast_result

            # Slow path: work-level cover relations.
            if not cover_work_ids:
                cover_work_ids = _extract_cover_work_ids(seed)
            if not cover_work_ids:
                return fast_result

            # Try release-level fallback first.
            if release_mbid and _release_cache is not None:
                result = self._check_release_for_cover(
                    release_mbid, title, recording_mbid, album_artist, _release_cache
                )
                if result:
                    return result

            search_title = seed.get("title") or _COVER_SUFFIX_RE.sub("", title).strip() or title
            recordings = self.mb.search_recordings(search_title, limit=50) or []

            earliest = None
            earliest_year = 9999
            earliest_unknown = None

            for rec in recordings:
                rid = rec.get("id")
                if not rid or rid == recording_mbid:
                    continue
                try:
                    details = self.mb.get_recording(rid, inc="artist-credits+releases+work-rels")
                except Exception:
                    continue

                rec_work_ids = _extract_work_ids(details)
                if not (rec_work_ids & cover_work_ids):
                    continue
                if _extract_cover_work_ids(details) & cover_work_ids:
                    continue

                rec_artist = _artist_from_credit(details.get("artist-credit", []) or rec.get("artist-credit", []))
                if not rec_artist:
                    continue
                if album_artist and _names_match(rec_artist, album_artist):
                    continue

                year = _year_from_recording(details)
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "high"}
                elif year is None and earliest_unknown is None:
                    earliest_unknown = {"artist": rec_artist, "year": None, "confidence": "high"}

            return earliest or earliest_unknown or fast_result
        except Exception as exc:
            logger.debug("Recording-relation detection failed for %s: %s", recording_mbid, exc)
            return fast_result

    def _check_release_for_cover(
        self,
        release_mbid: str,
        track_title: str,
        recording_mbid: Optional[str],
        album_artist: Optional[str],
        _release_cache: Dict[str, dict],
    ) -> Optional[Dict]:
        """Check if the album release has a track with cover relations."""
        if release_mbid in _release_cache:
            release = _release_cache[release_mbid]
        else:
            try:
                release = self.mb.get_release(
                    release_mbid, inc="recordings+artist-credits"
                )
                _release_cache[release_mbid] = release
            except Exception:
                return None

        canonical_target = _canonical_track_title(track_title)
        for medium in release.get("media", []) or []:
            for mb_track in medium.get("tracks", []) or []:
                rec = mb_track.get("recording") or {}
                candidate = rec.get("title") or mb_track.get("title") or ""
                if _canonical_track_title(candidate) != canonical_target:
                    continue
                release_rec_id = str(rec.get("id") or "").strip()
                if not release_rec_id or release_rec_id == (recording_mbid or "").strip():
                    continue
                return self._detect_via_recording_relation(
                    recording_mbid=release_rec_id,
                    track_id="",
                    title=track_title,
                    album_artist=album_artist,
                )
        return None

    def _detect_cover_hint_from_text(self, track_title: str, album_title: str) -> Optional[str]:
        """Infer probable original artist from track or album text."""
        text = f"{track_title or ''} {album_title or ''}".strip()
        if not text:
            return None
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

    def _find_original_recording(self, title: str, writer: str,
                                 album_artist: Optional[str] = None) -> Optional[Dict]:
        """Find earliest likely original recording for a title/writer pair."""
        try:
            search_title = _canonical_track_title(title) or title
            recordings = self.mb.search_recordings(search_title, limit=25) or []
            if not recordings:
                return None

            earliest = None
            earliest_year = 9999
            earliest_unknown = None

            for rec in recordings:
                rid = rec.get("id")
                if not rid:
                    continue
                try:
                    details = self.mb.get_recording(
                        rid, inc="artist-credits+releases+work-rels+artist-rels"
                    )
                except Exception:
                    continue

                # Check if this recording has the writer as composer/lyricist.
                writer_names: List[str] = []
                for rel in details.get("artist-relation-list", []) or []:
                    rt = str(rel.get("type", "")).lower()
                    if rt in ("composer", "writer", "lyricist"):
                        an = (rel.get("artist") or {}).get("name")
                        if an:
                            writer_names.append(an)
                for rel in details.get("work-relation-list", []) or []:
                    work = rel.get("work") or {}
                    for work_rel in work.get("artist-relation-list", []) or []:
                        rt = str(work_rel.get("type", "")).lower()
                        if rt in ("composer", "writer", "lyricist"):
                            an = (work_rel.get("artist") or {}).get("name")
                            if an:
                                writer_names.append(an)

                writer_names = _normalize_writer_credits(writer_names)
                writer_match = any(_names_match(writer, c) for c in writer_names)

                rec_artist = _artist_from_credit(
                    details.get("artist-credit", []) or rec.get("artist-credit", [])
                )
                if not rec_artist:
                    continue
                if album_artist and _names_match(rec_artist, album_artist):
                    continue

                year = _year_from_recording(details)
                if writer_match:
                    if year is not None and year < earliest_year:
                        earliest_year = year
                        earliest = {"artist": rec_artist, "year": year, "confidence": "medium"}
                    elif year is None and earliest_unknown is None:
                        earliest_unknown = {"artist": rec_artist, "year": None, "confidence": "medium"}
                elif year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "low"}
                elif year is None and earliest_unknown is None and earliest is None:
                    earliest_unknown = {"artist": rec_artist, "year": None, "confidence": "low"}

            return earliest or earliest_unknown
        except Exception as exc:
            logger.debug("Original recording lookup failed for '%s' by '%s': %s",
                         title, writer, exc)
            return None

    def _find_original_via_work_lookup(self, recording_mbid: str, title: str,
                                       album_artist: Optional[str] = None) -> Optional[Dict]:
        """Fallback: find original via work-linked recordings."""
        try:
            seed = self.mb.get_recording(recording_mbid, inc="work-rels+artist-credits")
            if not seed:
                return None

            work_ids: Set[str] = set()
            for rel in seed.get("work-relation-list", []) or []:
                if not isinstance(rel, dict):
                    continue
                if str(rel.get("type", "")).strip().lower() != "performance":
                    continue
                wid = (rel.get("work") or {}).get("id")
                if wid:
                    work_ids.add(wid)

            if not work_ids:
                return None

            canonical = _canonical_track_title(title) or title
            recordings = self.mb.search_recordings(canonical, limit=50) or []
            if not recordings:
                return None

            earliest = None
            earliest_year = 9999
            earliest_unknown = None

            for rec in recordings:
                rid = rec.get("id")
                if not rid or rid == recording_mbid:
                    continue
                try:
                    details = self.mb.get_recording(rid, inc="artist-credits+releases+work-rels")
                except Exception:
                    continue

                rec_work_ids = _extract_work_ids(details)
                if not (rec_work_ids & work_ids):
                    continue
                if _extract_cover_work_ids(details) & work_ids:
                    continue

                rec_artist = _artist_from_credit(
                    details.get("artist-credit", []) or rec.get("artist-credit", [])
                )
                if not rec_artist:
                    continue
                if album_artist and _names_match(rec_artist, album_artist):
                    continue

                year = _year_from_recording(details)
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "medium"}
                elif year is None and earliest_unknown is None:
                    earliest_unknown = {"artist": rec_artist, "year": None, "confidence": "low"}

            return earliest or earliest_unknown
        except Exception as exc:
            logger.debug("Work-based fallback failed for %s: %s", recording_mbid, exc)
            return None

    # ------------------------------------------------------------------
    # Writer & band-member helpers
    # ------------------------------------------------------------------

    def _collect_track_writers(self, tracks: List[Dict], album_artist: str) -> Dict[str, Dict]:
        """Collect writer/composer info for all tracks in an album."""
        result: Dict[str, Dict] = {}
        for track in tracks:
            tid = track.get("id")
            if not tid:
                continue
            writers = self._get_track_writers(track)
            title = track.get("title", "Unknown")
            if writers:
                result[tid] = {
                    "title": title,
                    "writers": writers,
                    "mbid": track.get("mbid"),
                    "track_artist": track.get("artist") or track.get("album_artist") or album_artist,
                }
        return result

    def _get_track_writers(self, track: Dict) -> List[str]:
        """Extract writer/composer information from track data."""
        writers: List[str] = []

        raw = track.get("writer")
        if raw:
            try:
                if isinstance(raw, str):
                    writers = json.loads(raw)
                elif isinstance(raw, list):
                    writers = raw
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: check DB if not in the track dict.
        if not writers and self.db_conn:
            try:
                cursor = self.db_conn.cursor()
                cursor.execute(
                    "SELECT writer FROM tracks WHERE id = %s",
                    (track.get("id"),),
                )
                row = cursor.fetchone()
                if row:
                    raw = row_get(row, "writer", 0, "")
                    if raw:
                        try:
                            writers = json.loads(raw) if isinstance(raw, str) else raw
                        except (json.JSONDecodeError, TypeError):
                            pass
            except Exception:
                pass

        if not writers:
            mbid = track.get("mbid") or self._resolve_recording_mbid(
                track,
                album_artist=track.get("album_artist") or track.get("artist"),
                album_title=track.get("album"),
            )
            if mbid:
                track["mbid"] = mbid
                writers = self._fetch_writers_from_musicbrainz(mbid)

        return _normalize_writer_credits(writers)

    def _fetch_writers_from_musicbrainz(self, mbid: str) -> List[str]:
        """Fetch writer/composer credits from MusicBrainz recording."""
        try:
            recording = self.mb.get_recording(mbid, inc="artist-rels+work-rels")
            if not recording:
                return []

            writers: List[str] = []
            for rel in recording.get("artist-relation-list", []) or []:
                rt = str(rel.get("type", "")).lower()
                if rt in ("composer", "lyricist", "writer"):
                    an = (rel.get("artist") or {}).get("name")
                    if an and an not in writers:
                        writers.append(an)

            for rel in recording.get("work-relation-list", []) or []:
                work = rel.get("work") or {}
                wid = work.get("id")
                work_rels = work.get("artist-relation-list", [])
                if wid and not work_rels:
                    try:
                        wd = self.mb.get_release(wid, inc="artist-rels")
                        work_rels = (wd or {}).get("artist-relation-list", [])
                    except Exception:
                        pass
                for work_rel in work_rels:
                    rt = str(work_rel.get("type", "")).lower()
                    if rt in ("composer", "lyricist", "writer"):
                        an = work_rel.get("artist", {}).get("name")
                        if an and an not in writers:
                            writers.append(an)
            return writers
        except Exception as exc:
            logger.debug("Failed to fetch writers from MB for %s: %s", mbid, exc)
            return []

    def _resolve_recording_mbid(self, track: Dict, album_artist: Optional[str] = None,
                                album_title: Optional[str] = None) -> Optional[str]:
        """Resolve a recording MBID for tracks with only release-level metadata."""
        existing = str(track.get("mbid") or "").strip()
        if existing:
            return existing

        title = str(track.get("title") or "").strip()
        if not title:
            return None

        search_artist = str(track.get("artist") or album_artist or "").strip()
        release_mbid = str(track.get("musicbrainz_album_mbid") or "").strip()
        target = _canonical_track_title(title)

        def _titles_match(left: str, right: str) -> bool:
            lt = _canonical_track_title(left)
            rt = _canonical_track_title(right)
            if not lt or not rt:
                return False
            return lt == rt or lt.startswith(rt) or rt.startswith(lt)

        def _artist_matches(artist_credit: List[Dict]) -> bool:
            if not search_artist:
                return True
            return _names_match(_artist_from_credit(artist_credit), search_artist)

        if release_mbid:
            try:
                release = self.mb.get_release(release_mbid, inc="recordings+artist-credits")
                if release:
                    for medium in release.get("media", []) or []:
                        for mb_track in medium.get("tracks", []) or []:
                            rec = mb_track.get("recording") or {}
                            ct = rec.get("title") or mb_track.get("title") or ""
                            if not _titles_match(title, ct):
                                continue
                            if _artist_matches(rec.get("artist-credit", []) or mb_track.get("artist-credit", [])):
                                resolved = str(rec.get("id") or "").strip()
                                if resolved:
                                    return resolved
            except Exception:
                pass

        try:
            kwargs = {"recording": title, "limit": 10}
            if search_artist:
                kwargs["artist"] = search_artist
            recordings = self.mb.search_recordings(**kwargs) or []
            for rec in recordings:
                ct = rec.get("title", "")
                if not _titles_match(title, ct):
                    continue
                if not _artist_matches(rec.get("artist-credit", []) or []):
                    continue
                resolved = str(rec.get("id") or "").strip()
                if resolved:
                    return resolved
        except Exception:
            pass
        return None

    def _get_band_members(self, artist: str) -> List[str]:
        """Fetch band members from MusicBrainz with caching."""
        if artist in self._band_members_cache:
            return self._band_members_cache[artist]

        members: List[str] = []
        try:
            # Search for the artist to get their MBID first.
            results = self.mb.search_artists(artist, limit=3) or []
            for result in results:
                if _names_match(result.get("name", ""), artist):
                    artist_mbid = result.get("id")
                    if not artist_mbid:
                        continue
                    details = self.mb.get_artist(artist_mbid, inc="artist-rels")
                    for rel in details.get("artist-relation-list", []) or []:
                        if str(rel.get("type", "")).lower() == "member of band":
                            an = (rel.get("artist") or {}).get("name")
                            if an and an not in members:
                                members.append(an)
                    break
        except Exception as exc:
            logger.debug("Failed to fetch band members for '%s': %s", artist, exc)

        self._band_members_cache[artist] = members
        return members

    def _is_writer_same_as_artist(self, writer: str, artist: str) -> bool:
        """Check if writer matches artist or is a known band member."""
        if _names_match(writer, artist):
            return True
        for member in self._get_band_members(artist):
            if _names_match(writer, member):
                return True
        return False

    def _artist_has_original(self, artist: str, title: str) -> bool:
        """Check if the artist already has a non-cover recording of *title* in the local DB."""
        if not self.db_conn or not artist or not title:
            return False
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                SELECT 1 FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
                  AND LOWER(title) = LOWER(%s)
                  AND COALESCE(is_cover, 0) = 0
                LIMIT 1
                """,
                (artist, title),
            )
            return cursor.fetchone() is not None
        except Exception:
            return False

    def _is_common_writer_for_artist(self, writer: str, artist: str,
                                      track_artist: Optional[str] = None,
                                      min_count: int = 2) -> bool:
        """Check if a writer appears frequently on tracks by this artist."""
        if not self.db_conn or not writer or not artist:
            return False

        lookup = track_artist if track_artist and not _names_match(track_artist, artist) else artist
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                SELECT writer FROM tracks
                WHERE (LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
                       OR LOWER(artist) = LOWER(%s))
                  AND writer IS NOT NULL AND writer != '' AND writer != '[]'
                """,
                (lookup, lookup),
            )
            rows = cursor.fetchall() or []
        except Exception:
            return False

        writer_norm = _normalize_name(writer)
        count = 0
        for row in rows:
            raw = row_get(row, "writer", 0, "")
            if not raw:
                continue
            try:
                names = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(names, list):
                    names = [str(names)]
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if any(_normalize_name(str(n)) == writer_norm for n in names):
                count += 1
                if count >= min_count:
                    return True
        return False

    def _writer_coverage_for_artist(self, artist: str) -> float:
        """Return fraction of tracks by *artist* that have a non-null writer field."""
        if not self.db_conn or not artist:
            return 1.0
        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN writer IS NOT NULL
                             AND TRIM(CAST(writer AS TEXT)) NOT IN ('', '[]', 'null', 'None')
                        THEN 1 ELSE 0 END) AS with_writer
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
                """,
                (artist,),
            )
            row = cursor.fetchone()
            if row:
                total = int(row_get(row, "total", 0, 0) or 0)
                with_writer = int(row_get(row, "with_writer", 1, 0) or 0)
                return float(with_writer) / float(total) if total else 1.0
        except Exception:
            pass
        return 1.0

    def _is_already_confirmed_cover(self, track: Dict) -> bool:
        """Return True when a track has all three confirmation signals and can be skipped."""
        if not self._is_cover_flagged(track):
            return False
        title = track.get("title", "")
        if not re.search(r'\([^)]+\s*cover\)\s*$', title, re.IGNORECASE):
            return False

        def _has_cover_genre(raw: str) -> bool:
            if not raw:
                return False
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return any(str(g).lower() == "cover" for g in parsed)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            return "cover" in raw.lower()

        if _has_cover_genre(track.get("genres") or "") or _has_cover_genre(track.get("musicbrainz_genres") or ""):
            return True
        if self.db_conn:
            try:
                cursor = self.db_conn.cursor()
                cursor.execute(
                    "SELECT genres, musicbrainz_genres FROM tracks WHERE id = %s",
                    (track.get("id"),),
                )
                row = cursor.fetchone()
                if row:
                    if _has_cover_genre(row_get(row, "genres", 0, "") or "") or \
                       _has_cover_genre(row_get(row, "musicbrainz_genres", 1, "") or ""):
                        return True
            except Exception:
                pass
        return False

    def _is_cover_flagged(self, track: Dict) -> bool:
        """Check if is_cover flag is truthy in various formats."""
        val = track.get("is_cover")
        if val in (None, 0, False, "0", "", "false", "no"):
            return False
        if isinstance(val, str) and val.strip().lower() in ("0", "false", "no", ""):
            return False
        return True

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    def _build_update(self, result: Dict, track: Dict) -> Dict:
        """Build a pending-update dict from a detection result."""
        return {
            "track_id": result["track_id"],
            "title": result["title"],
            "original_artist": result.get("original_artist", ""),
            "file_path": track.get("file_path"),
            "is_cover_reason": f"Cover detection: {result['confidence']} confidence, "
                               f"originally by {result.get('original_artist', 'unknown')}",
        }

    def _apply_cover_metadata_batch(self, updates: List[Dict], max_retries: int = 5) -> List[str]:
        """Persist cover metadata updates in a deterministic batch."""
        if not self.db_conn or not updates:
            return []

        conn = self.db_conn
        rows = sorted(
            [dict(u) for u in updates if u.get("track_id")],
            key=lambda x: str(x["track_id"]),
        )
        if not rows:
            return []

        delay = 0.15
        for attempt in range(max_retries):
            try:
                cursor = conn.cursor()
                successful: List[str] = []
                for update in rows:
                    tid = update["track_id"]
                    title = update.get("title", "")
                    orig = update.get("original_artist", "")
                    reason = update.get("is_cover_reason") or f"Originally by {orig}" if orig else "Cover detection"
                    new_title = self._build_cover_title(title, orig)

                    # Add "Cover" to musicbrainz_genres.
                    cursor.execute("SELECT musicbrainz_genres FROM tracks WHERE id = %s", (tid,))
                    row = cursor.fetchone()
                    mb_raw = row_get(row, "musicbrainz_genres", 0, "") or ""
                    try:
                        mb_list = json.loads(mb_raw) if mb_raw and mb_raw != "null" else []
                        if not isinstance(mb_list, list):
                            mb_list = []
                    except (json.JSONDecodeError, TypeError):
                        mb_list = []
                    if "Cover" not in [str(g).strip() for g in mb_list]:
                        mb_list.insert(0, "Cover")
                        cursor.execute(
                            "UPDATE tracks SET musicbrainz_genres = %s WHERE id = %s",
                            (json.dumps(mb_list), tid),
                        )

                    if new_title != title:
                        cursor.execute("UPDATE tracks SET title = %s WHERE id = %s", (new_title, tid))
                    cursor.execute(
                        "UPDATE tracks SET is_cover = 1, is_cover_reason = %s, "
                        "original_cover_artist = %s WHERE id = %s",
                        (reason, orig, tid),
                    )
                    successful.append(tid)

                conn.commit()
                return successful
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt < max_retries - 1:
                    sleep_for = min(delay * (2 ** attempt), 2.0)
                    logger.warning("Cover batch transient DB error, retry %d/%d in %.2fs: %s",
                                   attempt + 1, max_retries, sleep_for, exc)
                    time.sleep(sleep_for)
                else:
                    raise
        return []

    # ------------------------------------------------------------------
    # File metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cover_title(title: str, original_artist: Optional[str]) -> str:
        if _COVER_SUFFIX_RE.search(title or ""):
            return title
        if original_artist:
            return f"{title} ({original_artist} Cover)"
        return title

    @staticmethod
    def _update_file_metadata(file_path: str, title: str, additional_genres: List[str]) -> bool:
        """Update audio file tags with cover attribution."""
        try:
            from mutagen.mp3 import MP3
            from mutagen.flac import FLAC
            from mutagen.id3 import ID3, TIT2, TCON

            path = Path(file_path)
            if path.suffix.lower() == ".mp3":
                audio = MP3(file_path, ID3=ID3)
                audio.tags["TIT2"] = TIT2(encoding=3, text=title)
                current = list(audio.tags.get("TCON", TCON(encoding=3, text=[])).text)
                for g in additional_genres:
                    if g not in current:
                        current.append(g)
                audio.tags["TCON"] = TCON(encoding=3, text=current)
                audio.save()
            elif path.suffix.lower() == ".flac":
                audio = FLAC(file_path)
                audio["title"] = title
                current = audio.get("genre", [])
                if isinstance(current, str):
                    current = [current]
                for g in additional_genres:
                    if g not in current:
                        current.append(g)
                audio["genre"] = current
                audio.save()
            return True
        except Exception as exc:
            logger.error("Failed to update file metadata for %s: %s", file_path, exc)
            return False
