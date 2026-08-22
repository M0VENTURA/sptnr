"""
Cover song detection module — implementation class.

Detects cover songs by analyzing songwriter/composer data from MusicBrainz,
then attributes the original artist and updates track metadata accordingly.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from api_clients.musicbrainz_http import MusicBrainzHttpClient
from services.enrichment.musicbrainz_service import get_shared_mb_client
from db.repositories.cover_detection_repository import (
    apply_cover_metadata_batch,
    get_track_genres,
    get_track_writers_from_db,
    is_common_writer_for_artist,
    track_has_original_by_artist,
    writer_coverage_for_artist,
)
from helpers.musicbrainz_helpers import (
    artist_from_credit,
    extract_cover_work_ids,
    extract_work_ids,
    year_from_recording,
)
from helpers.normalization_service import (
    canonical_track_title,
    names_match,
    normalize_name,
    normalize_writer_credits,
)

logger = structlog.get_logger(__name__)

# Matches a trailing "(X Cover)" annotation in a track title.
_COVER_SUFFIX_RE = re.compile(r'\s*\([^)]+\s+cover\)\s*$', re.IGNORECASE)

# =============================================================================
# THREAD-SAFE SHARED CACHE
# =============================================================================
_CACHE_LOCK = threading.Lock()
_RELEASE_TRACKLIST_CACHE: dict[str, dict[str, Any]] = {}

# Compilation artist names used to decide when per-track-artist lookups are needed.
_COMPILATION_ARTIST_NAMES = frozenset({
    'various artists', 'various', 'v/a', 'va', 'compilation', 'soundtrack',
})

COVER_RECHECK_DAYS = 90


def _checked_fresh(checked_ts: Any) -> bool:
    """True when the cover-verdict timestamp is within the recheck window."""
    if not checked_ts:
        return False
    try:
        if isinstance(checked_ts, str):
            checked_ts = datetime.fromisoformat(str(checked_ts).replace("Z", "+00:00"))
        if checked_ts.tzinfo is None:
            checked_ts = checked_ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - checked_ts).days < COVER_RECHECK_DAYS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CoverDetector
# ---------------------------------------------------------------------------

class CoverDetector:
    """Detect and attribute cover songs using MusicBrainz relations and writer/composer data."""

    def __init__(self, db_connection: Any = None):
        # ✅ Use the shared client singleton to enforce global rate limits & connection pooling
        self.mb = get_shared_mb_client()
        self.db_conn = db_connection
        self._band_members_cache: dict[str, list[str]] = {}
        self._mem_lock = threading.Lock()

    def detect_covers_for_album(
        self,
        album: str,
        artist: str,
        tracks: list[dict[str, Any]],
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Detect cover songs in an album."""
        logger.info("Starting cover detection", album=album, artist=artist, track_count=len(tracks))

        for track in tracks:
            track.setdefault("album", album)
            track.setdefault("album_artist", artist)

        seen_track_ids: set[str] = set()
        cover_results: list[dict[str, Any]] = []
        pending_updates: list[dict[str, Any]] = []

        _release_cache: dict[str, dict[str, Any]] = {}
        _fresh_skipped: set[str] = set()
        
        if not force:
            checked_map = self._load_cover_checked_map([t.get("id") for t in tracks])
            for track in tracks:
                tid = track.get("id")
                if not tid:
                    continue
                if _checked_fresh(track.get("cover_last_checked") or checked_map.get(tid)):
                    _fresh_skipped.add(tid)
                    seen_track_ids.add(tid)
                    continue
                if self._is_already_confirmed_cover(track) or self._cover_has_original_artist(track):
                    seen_track_ids.add(tid)

        track_writers = self._collect_track_writers(tracks, artist, seen_track_ids)

        # Step 2: ISRC-based matching
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

        # Step 3: MusicBrainz recording-relation detection
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

        # Step 4: writer-based detection
        is_compilation = artist.lower() in _COMPILATION_ARTIST_NAMES
        writer_skip_all = False
        
        if not is_compilation and track_writers:
            coverage = writer_coverage_for_artist(self.db_conn, artist)
            if coverage < 0.10:
                logger.info("Writer coverage low — skipping writer-based detection", artist=artist, coverage_pct=coverage * 100)
                writer_skip_all = True

        track_artist_coverage_cache: dict[str, float] = {}

        for tid, info in track_writers.items():
            if tid in seen_track_ids:
                continue
            if writer_skip_all:
                break

            if is_compilation:
                ta = info.get("track_artist", "") or ""
                if ta not in track_artist_coverage_cache:
                    track_artist_coverage_cache[ta] = writer_coverage_for_artist(self.db_conn, ta)
                if track_artist_coverage_cache[ta] < 0.10:
                    continue

            for writer in info.get("writers", []):
                if self._is_writer_same_as_artist(writer, artist):
                    continue
                if track_has_original_by_artist(self.db_conn, artist, info["title"]):
                    break
                if is_common_writer_for_artist(
                    self.db_conn, writer, artist, track_artist=info.get("track_artist"),
                ):
                    break

                search_title = _COVER_SUFFIX_RE.sub("", info["title"]).strip() or info["title"]
                original = self._find_original_recording(search_title, writer, album_artist=artist)
                if not original:
                    continue
                if names_match(original.get("artist", ""), artist):
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

        # Step 5: heuristic title/text-based detection
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
                continue
            title = track.get("title", "")
            hint = self._detect_cover_hint_from_text(title, album)
            if not hint:
                continue

            base_title = _COVER_SUFFIX_RE.sub("", title).strip()
            if base_title and base_title.lower() != title.lower() and track_has_original_by_artist(self.db_conn, artist, base_title):
                continue

            resolved_artist = hint
            resolved_year: int | None = None
            resolved_confidence = "low"
            resolved_writer = ""
            writers_for_track = (track_writers.get(tid) or {}).get("writers", [])
            
            if writers_for_track and base_title:
                for wr in writers_for_track:
                    if self._is_writer_same_as_artist(wr, artist):
                        continue
                    mb_orig = self._find_original_recording(base_title, wr, album_artist=artist)
                    if mb_orig and not names_match(mb_orig.get("artist", ""), artist):
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

        # Step 6: work-based fallback
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

        if pending_updates:
            successful = set(apply_cover_metadata_batch(self.db_conn, pending_updates))
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

        try:
            assessed = [
                str(t.get("id")) for t in tracks
                if t.get("id") and str(t.get("id")) not in _fresh_skipped
            ]
            if assessed:
                from sqlalchemy import text as _text
                from db.engine import db_session as _db_session
                ids_placeholders = ", ".join(f":tid{i}" for i in range(len(assessed)))
                with _db_session() as session:
                    session.execute(
                        _text(f"UPDATE tracks SET cover_last_checked = :checked WHERE id IN ({ids_placeholders})"),
                        {"checked": datetime.now(timezone.utc).isoformat(),
                         **{f"tid{i}": t for i, t in enumerate(assessed)}},
                    )
        except Exception as exc:
            logger.debug("cover_last_checked persist failed", error=str(exc))

        logger.info("Cover detection complete", album=album, found_count=len(cover_results))
        return cover_results

    def is_cover(self, title: str, artist: str, composer: str | None = None,
                 writer: str | None = None) -> bool:
        if not title:
            return False
        if _COVER_SUFFIX_RE.search(title):
            return True
        if "cover" in title.lower():
            return True
        if composer or writer:
            candidates = [c for c in [composer, writer] if c]
            for c in candidates:
                if not names_match(c, artist):
                    return True
        return False

    def _detect_via_isrc(self, isrc: str, track_id: str, title: str,
                         album_artist: str) -> dict[str, Any] | None:
        try:
            recordings = self.mb.lookup_by_isrc(isrc, inc="artist-credits+releases+work-rels+recording-rels")
            if not recordings:
                return None

            earliest: dict[str, Any] | None = None
            earliest_year = 9999

            for rec in recordings:
                rec_id = rec.get("id")
                if not rec_id:
                    continue
                rec_artist = artist_from_credit(rec.get("artist-credit", []))
                if not rec_artist:
                    continue
                if names_match(rec_artist, album_artist):
                    continue
                year = year_from_recording(rec)
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "high"}
                elif year is None and earliest is None:
                    earliest = {"artist": rec_artist, "year": None, "confidence": "high"}

            if earliest:
                logger.info("ISRC-based cover detected", track=title, original_artist=earliest["artist"])
            return earliest
        except Exception as exc:
            logger.debug("ISRC lookup failed", isrc=isrc, error=str(exc))
            return None

    def _detect_via_recording_relation(
        self,
        recording_mbid: str,
        track_id: str,
        title: str,
        album_artist: str,
        release_mbid: str = "",
        _release_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        fast_result = None
        fast_orig_id = None

        try:
            seed = self.mb.get_recording(
                recording_mbid,
                inc="work-rels+recording-rels+artist-credits+releases",
            )
            if not seed:
                return None

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
                orig_artist = artist_from_credit(orig_rec.get("artist-credit", []))
                orig_year = year_from_recording(orig_rec)
                if not orig_artist or orig_year is None:
                    try:
                        details = self.mb.get_recording(orig_id, inc="artist-credits+releases")
                        if not orig_artist:
                            orig_artist = artist_from_credit(details.get("artist-credit", []))
                        if orig_year is None:
                            orig_year = year_from_recording(details)
                    except Exception:
                        pass
                if not orig_artist:
                    continue
                if album_artist and names_match(orig_artist, album_artist):
                    continue
                logger.debug("Cover via recording relation", track=title, original_artist=orig_artist)
                fast_result = {"artist": orig_artist, "year": orig_year, "confidence": "high"}
                fast_orig_id = orig_id
                break

            cover_work_ids: set[str] = set()
            if fast_result and fast_orig_id:
                try:
                    chain = self.mb.get_recording(fast_orig_id, inc="work-rels")
                    chained_cover = extract_cover_work_ids(chain)
                    if chained_cover:
                        cover_work_ids = chained_cover
                    else:
                        return fast_result
                except Exception:
                    return fast_result

            if not cover_work_ids:
                cover_work_ids = extract_cover_work_ids(seed)
            if not cover_work_ids:
                return fast_result

            if release_mbid and _release_cache is not None:
                result = self._check_release_for_cover(
                    release_mbid, title, recording_mbid, album_artist, _release_cache
                )
                if result:
                    return result

            search_title = seed.get("title") or _COVER_SUFFIX_RE.sub("", title).strip() or title
            recordings = self.mb.search_recordings(search_title, limit=15) or []

            earliest = None
            earliest_year = 9999
            earliest_unknown = None

            _inspected = 0
            for rec in recordings:
                if _inspected >= 12:
                    break
                _inspected += 1
                rid = rec.get("id")
                if not rid or rid == recording_mbid:
                    continue
                try:
                    details = self.mb.get_recording(rid, inc="artist-credits+releases+work-rels")
                except Exception:
                    continue

                rec_work_ids = extract_work_ids(details)
                if not (rec_work_ids & cover_work_ids):
                    continue
                if extract_cover_work_ids(details) & cover_work_ids:
                    continue

                rec_artist = artist_from_credit(details.get("artist-credit", []) or rec.get("artist-credit", []))
                if not rec_artist:
                    continue
                if album_artist and names_match(rec_artist, album_artist):
                    continue

                year = year_from_recording(details)
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "high"}
                elif year is None and earliest_unknown is None:
                    earliest_unknown = {"artist": rec_artist, "year": None, "confidence": "high"}

            return earliest or earliest_unknown or fast_result
        except Exception as exc:
            logger.debug("Recording-relation detection failed", mbid=recording_mbid, error=str(exc))
            return fast_result

    def _check_release_for_cover(
        self,
        release_mbid: str,
        track_title: str,
        recording_mbid: str | None,
        album_artist: str | None,
        _release_cache: dict[str, Any],
    ) -> dict[str, Any] | None:
        with _CACHE_LOCK:
            cached_release = _RELEASE_TRACKLIST_CACHE.get(release_mbid)

        if cached_release is not None:
            release = cached_release
        else:
            try:
                release = self.mb.get_release(
                    release_mbid, inc="recordings+artist-credits"
                ) or {}
                with _CACHE_LOCK:
                    _RELEASE_TRACKLIST_CACHE[release_mbid] = release
            except Exception:
                return None

        canonical_target = canonical_track_title(track_title)
        for medium in release.get("media", []) or []:
            for mb_track in medium.get("tracks", []) or []:
                rec = mb_track.get("recording") or {}
                candidate = rec.get("title") or mb_track.get("title") or ""
                if canonical_track_title(candidate) != canonical_target:
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

    def _detect_cover_hint_from_text(self, track_title: str, album_title: str) -> str | None:
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
                                 album_artist: str | None = None) -> dict[str, Any] | None:
        try:
            search_title = canonical_track_title(title) or title
            recordings = self.mb.search_recordings(search_title, limit=10) or []
            if not recordings:
                return None

            earliest = None
            earliest_year = 9999
            earliest_unknown = None

            _inspected = 0
            for rec in recordings:
                if _inspected >= 8:
                    break
                _inspected += 1
                rid = rec.get("id")
                if not rid:
                    continue
                try:
                    details = self.mb.get_recording(
                        rid, inc="artist-credits+releases+work-rels+artist-rels"
                    )
                except Exception:
                    continue

                writer_names: list[str] = []
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

                writer_names = normalize_writer_credits(writer_names)
                writer_match = any(names_match(writer, c) for c in writer_names)

                rec_artist = artist_from_credit(
                    details.get("artist-credit", []) or rec.get("artist-credit", [])
                )
                if not rec_artist:
                    continue
                if album_artist and names_match(rec_artist, album_artist):
                    continue

                year = year_from_recording(details)
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
            logger.debug("Original recording lookup failed", track=title, writer=writer, error=str(exc))
            return None

    def _find_original_via_work_lookup(self, recording_mbid: str, title: str,
                                       album_artist: str | None = None) -> dict[str, Any] | None:
        try:
            seed = self.mb.get_recording(recording_mbid, inc="work-rels+artist-credits")
            if not seed:
                return None

            work_ids: set[str] = set()
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

            canonical = canonical_track_title(title) or title
            recordings = self.mb.search_recordings(canonical, limit=15) or []
            if not recordings:
                return None

            earliest = None
            earliest_year = 9999
            earliest_unknown = None

            _inspected = 0
            for rec in recordings:
                if _inspected >= 12:
                    break
                _inspected += 1
                rid = rec.get("id")
                if not rid or rid == recording_mbid:
                    continue
                try:
                    details = self.mb.get_recording(rid, inc="artist-credits+releases+work-rels")
                except Exception:
                    continue

                rec_work_ids = extract_work_ids(details)
                if not (rec_work_ids & work_ids):
                    continue
                if extract_cover_work_ids(details) & work_ids:
                    continue

                rec_artist = artist_from_credit(
                    details.get("artist-credit", []) or rec.get("artist-credit", [])
                )
                if not rec_artist:
                    continue
                if album_artist and names_match(rec_artist, album_artist):
                    continue

                year = year_from_recording(details)
                if year is not None and year < earliest_year:
                    earliest_year = year
                    earliest = {"artist": rec_artist, "year": year, "confidence": "medium"}
                elif year is None and earliest_unknown is None:
                    earliest_unknown = {"artist": rec_artist, "year": None, "confidence": "low"}

            return earliest or earliest_unknown
        except Exception as exc:
            logger.debug("Work-based fallback failed", mbid=recording_mbid, error=str(exc))
            return None

    def _load_cover_checked_map(self, track_ids: list[Any]) -> dict[str, Any]:
        ids = [str(t) for t in track_ids if t]
        if not ids:
            return {}
        try:
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            ids_placeholders = ", ".join(f":tid{i}" for i in range(len(ids)))
            with _db_session() as session:
                rows = session.execute(
                    _text(f"SELECT id, cover_last_checked FROM tracks WHERE id IN ({ids_placeholders})"),
                    {f"tid{i}": t for i, t in enumerate(ids)},
                ).fetchall() or []
            return {
                str(r[0]): r[1]
                for r in rows
            }
        except Exception as exc:
            logger.debug("cover_last_checked load failed", error=str(exc))
            return {}

    def _collect_track_writers(
        self, tracks: list[dict[str, Any]], album_artist: str, seen_track_ids: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        seen_track_ids = seen_track_ids or set()
        result: dict[str, dict[str, Any]] = {}
        for track in tracks:
            tid = track.get("id")
            if not tid or tid in seen_track_ids:
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

    def _get_track_writers(self, track: dict[str, Any]) -> list[str]:
        writers: list[str] = []

        raw = track.get("writer")
        if raw:
            try:
                if isinstance(raw, str):
                    writers = json.loads(raw)
                elif isinstance(raw, list):
                    writers = raw
            except (json.JSONDecodeError, TypeError):
                pass

        if not writers:
            writers = get_track_writers_from_db(None, track.get("id", ""))

        if not writers:
            mbid = track.get("mbid") or self._resolve_recording_mbid(
                track,
                album_artist=track.get("album_artist") or track.get("artist"),
                album_title=track.get("album"),
            )
            if mbid:
                track["mbid"] = mbid
                writers = self._fetch_writers_from_musicbrainz(mbid)

        return normalize_writer_credits(writers)

    def _fetch_writers_from_musicbrainz(self, mbid: str) -> list[str]:
        try:
            recording = self.mb.get_recording(mbid, inc="artist-rels+work-rels")
            if not recording:
                return []

            writers: list[str] = []
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
            logger.debug("Failed to fetch writers from MB", mbid=mbid, error=str(exc))
            return []

    def _resolve_recording_mbid(self, track: dict[str, Any], album_artist: str | None = None,
                                album_title: str | None = None) -> str | None:
        existing = str(track.get("mbid") or "").strip()
        if existing:
            return existing

        title = str(track.get("title") or "").strip()
        if not title:
            return None

        search_artist = str(track.get("artist") or album_artist or "").strip()
        release_mbid = str(track.get("musicbrainz_album_mbid") or "").strip()
        target = canonical_track_title(title)

        def _titles_match(left: str, right: str) -> bool:
            lt = canonical_track_title(left)
            rt = canonical_track_title(right)
            if not lt or not rt:
                return False
            return lt == rt or lt.startswith(rt) or rt.startswith(lt)

        def _artist_matches(artist_credit: list[dict[str, Any]]) -> bool:
            if not search_artist:
                return True
            return names_match(artist_from_credit(artist_credit), search_artist)

        if release_mbid:
            try:
                with _CACHE_LOCK:
                    release = _RELEASE_TRACKLIST_CACHE.get(release_mbid)
                if release is None:
                    release = self.mb.get_release(release_mbid, inc="recordings+artist-credits") or {}
                    with _CACHE_LOCK:
                        _RELEASE_TRACKLIST_CACHE[release_mbid] = release
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
            kwargs: dict[str, Any] = {"recording": title, "limit": 10}
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

    def _get_band_members(self, artist: str) -> list[str]:
        if artist in self._band_members_cache:
            return self._band_members_cache[artist]

        members: list[str] = []
        try:
            results = self.mb.search_artists(artist, limit=3) or []
            for result in results:
                if names_match(result.get("name", ""), artist):
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
            logger.debug("Failed to fetch band members", artist=artist, error=str(exc))

        self._band_members_cache[artist] = members
        return members

    def _is_writer_same_as_artist(self, writer: str, artist: str) -> bool:
        if names_match(writer, artist):
            return True
        for member in self._get_band_members(artist):
            if names_match(writer, member):
                return True
        return False

    def _is_already_confirmed_cover(self, track: dict[str, Any]) -> bool:
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
            db_genres = get_track_genres(self.db_conn, track.get("id", ""))
            if _has_cover_genre(db_genres.get("genres") or "") or \
               _has_cover_genre(db_genres.get("musicbrainz_genres") or ""):
                return True
        return False

    def _is_cover_flagged(self, track: dict[str, Any]) -> bool:
        val = track.get("is_cover")
        if val in (None, 0, False, "0", "", "false", "no"):
            return False
        if isinstance(val, str) and val.strip().lower() in ("0", "false", "no", ""):
            return False
        return True

    @staticmethod
    def _cover_has_original_artist(track: dict[str, Any]) -> bool:
        if not track:
            return False
        if track.get("cover_manual_override"):
            return True
        cover_val = track.get("is_cover")
        if cover_val in (None, 0, False, "0", "", "false", "no"):
            return False
        original = str(track.get("original_cover_artist") or "").strip()
        return bool(original)

    def _build_update(self, result: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
        return {
            "track_id": result["track_id"],
            "title": result["title"],
            "original_artist": result.get("original_artist", ""),
            "file_path": track.get("file_path"),
            "is_cover_reason": f"Cover detection: {result['confidence']} confidence, "
                               f"originally by {result.get('original_artist', 'unknown')}",
        }

    @staticmethod
    def _build_cover_title(title: str, original_artist: str | None) -> str:
        if _COVER_SUFFIX_RE.search(title or ""):
            return title
        if original_artist:
            return f"{title} ({original_artist} Cover)"
        return title

    @staticmethod
    def _update_file_metadata(file_path: str, title: str, additional_genres: list[str]) -> bool:
        try:
            from services.metadata.tag_file_service import write_tags_to_file

            tags: dict[str, Any] = {"title": title}
            current_genres = additional_genres or []
            if current_genres:
                tags["genre"] = "; ".join(current_genres)

            return write_tags_to_file(file_path, tags)
        except Exception as exc:
            logger.error("Failed to update file metadata", file_path=file_path, error=str(exc))
            return False
