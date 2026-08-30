"""
MusicBrainz enrichment service.

Owns MusicBrainz interpretation/business rules:
- title/version normalisation
- single detection
- release-group matching
- release suggestion extraction
- relationship interpretation
- clean-name lookup

✅ No DB writes
✅ No track mutation
✅ Pure enrichment + lookup
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

import structlog

try:  
    from rapidfuzz import fuzz as _rapidfuzz_fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:
    import difflib as _difflib
    _HAVE_RAPIDFUZZ = False

from api_clients.musicbrainz_http import (
    MUSICBRAINZ_UUID_RE,
    MusicBrainzHttpClient,
    escape_lucene_special_chars,
)

from helpers.normalization_service import (
    normalize_string,
    normalize_title_for_lookup,
    normalize_title_for_lucene_query,
    normalize_title_for_mbid_match,
    strip_featured_artist,
    strip_single_release_suffix,
    strip_search_keywords,
    edition_annotations_compatible,
)

logger = structlog.get_logger(__name__)

_COMPARE_LIBRARY_TRACKS_SQL = """
    SELECT id, title, track_number, disc_number, artist, year,
           mbid, file_path, duration, mb_ignored_fields
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
"""


def _similarity(a: str, b: str) -> float:
    """String similarity on a 0-1 scale (shared ``fuzzy_match_score``)."""
    from services.popularity.popularity_math import fuzzy_match_score
    return fuzzy_match_score(a, b)


def _mbid_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _HAVE_RAPIDFUZZ:
        return _rapidfuzz_fuzz.token_sort_ratio(a, b) / 100.0
    return _difflib.SequenceMatcher(None, a, b).ratio()


CACHE_FILE = "/tmp/mbid_cache.json" if os.path.exists("/tmp") else "mbid_cache.json"

_CACHE_IO_LOCK = threading.Lock()
_INIT_LOCK = threading.Lock()

_MB_BATCH_CHUNK = 20
_MB_BATCH_SIMILARITY_FLOOR = 0.6

# Single-track suggested-MBID cache bounds (items 4 & 5 of the review):
# - _MBID_CACHE_SIMILARITY_FLOOR: same floor as the batch path — a weak
#   early match must not be cached permanently (a bad match poisoned the
#   track's identity forever before).
# - _MBID_CACHE_TTL_SECONDS: cached entries are re-validated after this long
#   (MusicBrainz merges/edits recordings, so a forever-cached MBID can go
#   stale).
# - _MBID_CACHE_MAX_SIZE: FIFO size cap so /tmp/mbid_cache.json cannot grow
#   without bound across long scans.
_MBID_CACHE_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
_MBID_CACHE_MAX_SIZE = 5000


# =============================================================================
# HELPERS
# =============================================================================

def build_artist_credit_string(artist_credit: list[Any]) -> str:
    result = ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            result += credit.get('name', '')
            result += credit.get('joinphrase', '')
        else:
            result += str(credit)
    return result.strip()


def primary_album_artist(artist_credit: list[Any] | str) -> str:
    if isinstance(artist_credit, list) and artist_credit:
        first = artist_credit[0]
        if isinstance(first, dict):
            return str(first.get("name") or "").strip()
        return str(first or "").strip()
    if isinstance(artist_credit, str):
        return artist_credit.strip()
    return ""


def calculate_match_score(mb_title: str, mb_artist_credit: list[Any] | str, local_album: str, local_artist: str) -> float:
    title_sim = _similarity(normalize_string(local_album), normalize_string(mb_title))

    artist_name = ""
    if isinstance(mb_artist_credit, list) and mb_artist_credit:
        if isinstance(mb_artist_credit[0], dict):
            artist_name = mb_artist_credit[0].get("name", "")
    elif isinstance(mb_artist_credit, str):
        artist_name = mb_artist_credit

    artist_sim = _similarity(normalize_string(local_artist), normalize_string(artist_name))

    return (title_sim * 0.6) + (artist_sim * 0.4)


def _parse_secondary_types(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    return []


def _artist_lookup_candidates(artist: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for candidate in (artist or "", strip_featured_artist(artist or "")):
        key = (candidate or "").casefold().strip()
        if candidate and key not in seen:
            candidates.append(candidate)
            seen.add(key)
    return candidates


# =============================================================================
# MAIN SERVICE
# =============================================================================

_SHARED_MB_CLIENT: MusicBrainzHttpClient | None = None

def get_shared_mb_client() -> MusicBrainzHttpClient:
    """Return the process-wide shared ``MusicBrainzHttpClient`` singleton."""
    global _SHARED_MB_CLIENT
    if _SHARED_MB_CLIENT is None:
        with _INIT_LOCK:
            if _SHARED_MB_CLIENT is None:
                _SHARED_MB_CLIENT = MusicBrainzHttpClient(enabled=True)
    return _SHARED_MB_CLIENT


def _recording_matches_album(recording: dict[str, Any], album: str) -> bool:
    if not album or not recording:
        return False
    album = str(album).strip().lower()
    if not album:
        return False
    for release in recording.get("releases") or []:
        candidates = [release.get("title") or ""]
        rg = release.get("release-group") or {}
        if rg.get("title"):
            candidates.append(rg["title"])
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if candidate and _similarity(candidate, album) >= 0.6:
                return True
    return False


def _first_isrc(recording: dict[str, Any]) -> str | None:
    from helpers.normalization_service import normalize_isrc
    isrcs = recording.get("isrcs") or recording.get("isrc-list") or []
    if isinstance(isrcs, list):
        for raw in isrcs:
            value = normalize_isrc(raw)
            if value:
                return value
    value = normalize_isrc(recording.get("isrc"))
    return value or None


class MusicBrainzService:

    def __init__(self, http_client: MusicBrainzHttpClient | None = None, enabled: bool = True):
        self.enabled = enabled
        self.http = http_client or MusicBrainzHttpClient(enabled=enabled)
        self._artist_singles_cache: dict[str, list[dict[str, Any]]] = {}
        self._mem_lock = threading.Lock()
        self._mbid_cache = self._load_cache()

    # -----------------------------------------------------------------------------
    # CACHE
    # -----------------------------------------------------------------------------

    def _load_cache(self) -> dict[str, Any]:
        with _CACHE_IO_LOCK:
            try:
                if os.path.exists(CACHE_FILE):
                    with open(CACHE_FILE, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        out: dict[str, Any] = {}
                        for key, value in raw.items():
                            if (
                                isinstance(value, (list, tuple))
                                and len(value) >= 2
                                and str(value[0] or "").strip()
                            ):
                                out[key] = value
                        return out
            except Exception:
                pass
            return {}

    def _save_cache(self) -> None:
        # Safely clone the cache so we don't hold the memory lock during slow disk I/O
        with self._mem_lock:
            data_to_save = dict(self._mbid_cache)

        with _CACHE_IO_LOCK:
            try:
                # Atomic write via temp-then-rename: a second process writing
                # concurrently sees either the old or the new file, never a
                # truncated/corrupted half-written JSON.
                tmp_path = f"{CACHE_FILE}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data_to_save, f)
                os.replace(tmp_path, CACHE_FILE)
            except Exception:
                pass

    def _cache_key(self, title: str, artist: str) -> str:
        return f"{artist.lower()}::{title.lower()}"

    # -----------------------------------------------------------------------------
    # CORE LOOKUPS
    # -----------------------------------------------------------------------------

    def get_suggested_mbid(self, title: str, artist: str, limit: int = 5) -> tuple[str, float]:
        if not self.enabled:
            return "", 0.0

        cache_key = self._cache_key(title, artist)
        now = time.time()

        with self._mem_lock:
            cached = self._mbid_cache.get(cache_key)
            if isinstance(cached, (list, tuple)) and len(cached) >= 2:
                # Entry shape: (mbid, score, cached_at).  Legacy entries may
                # lack the timestamp — treat them as valid (no expiry info).
                mbid, score = str(cached[0] or ""), float(cached[1] or 0)
                cached_at = float(cached[2]) if len(cached) >= 3 else None
                if mbid and (
                    cached_at is None or (now - cached_at) < _MBID_CACHE_TTL_SECONDS
                ):
                    return (mbid, round(score, 3))

        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = f'recording:"{escape_lucene_special_chars(query_title)}" AND artist:"{escape_lucene_special_chars(artist)}"'

        try:
            recordings = self.http.search_recordings(query, limit=limit)

            best_mbid = ""
            best_score = 0.0
            norm_title = normalize_title_for_mbid_match(title)

            for rec in recordings:
                rec_title = str(rec.get("title") or "")
                if not edition_annotations_compatible(title, rec_title):
                    continue
                sim = _mbid_similarity(
                    norm_title, normalize_title_for_mbid_match(rec_title)
                )

                if sim > best_score:
                    best_score = sim
                    best_mbid = rec.get("id", "")

            result = (best_mbid, round(best_score, 3))

            # Only cache a match that clears the similarity floor — a weak
            # early match (e.g. 0.1) must not be cached permanently and
            # poison the track's identity.  Same floor as the batch path.
            if best_mbid and best_score >= _MBID_CACHE_SIMILARITY_FLOOR:
                with self._mem_lock:
                    self._mbid_cache[cache_key] = (best_mbid, round(best_score, 3), now)
                    # FIFO size cap — drop oldest entries past the bound.
                    while len(self._mbid_cache) > _MBID_CACHE_MAX_SIZE:
                        try:
                            self._mbid_cache.pop(next(iter(self._mbid_cache)))
                        except (StopIteration, RuntimeError):
                            break
                self._save_cache()

            logger.debug(
                "MB lookup suggested MBID",
                artist=artist, track=title, mbid=best_mbid or "-", sim=best_score, candidates=len(recordings),
            )
            return result
        except Exception as exc:
            logger.debug("MB lookup search failed", artist=artist, track=title, error=str(exc))
            return "", 0.0

    def lookup_recording_metadata(self, title: str, artist: str) -> dict[str, Any]:
        if not title or not artist:
            return {}

        try:
            mbid, confidence = self.get_suggested_mbid(title, artist)

            if not mbid:
                return {}

            recording = self.http.get_recording(mbid, inc="artist-credits+releases")

            if not recording:
                return {}

            return self._recording_to_metadata(recording, mbid, confidence)

        except Exception as e:
            logger.debug("Recording metadata lookup failed", error=str(e), exc_info=True)
            return {}

    def _recording_to_metadata(self, recording: dict[str, Any], mbid: str, confidence: float) -> dict[str, Any]:
        credits = recording.get("artist-credit", [])
        rec_artist = ""
        rec_artist_mbid = ""

        if credits:
            first = credits[0]
            rec_artist = first.get("name") if isinstance(first, dict) else str(first)
            if isinstance(first, dict):
                rec_artist_mbid = (
                    (first.get("artist") or {}).get("id")
                    if isinstance(first.get("artist"), dict)
                    else ""
                ) or ""

        release = (recording.get("releases") or [None])[0]

        # ── Writer(s) from the recording's work-rels ──────────────────────
        # The recording's "performance"/"recording of" relation points to its
        # WORK; the work's own composer/writer/lyricist relations name the
        # writers.  Extracting them here means the album batch supplies the
        # writer WITHOUT a second per-track get_composers_for_recording call.
        writers: list[str] = []
        work_mbid: str | None = None
        try:
            for rel in recording.get("relations") or []:
                if not isinstance(rel, dict):
                    continue
                rel_type = str(rel.get("type") or "").lower()
                work = rel.get("work") or {}
                if rel_type in ("performance", "recording of") and work:
                    work_mbid = str(work.get("id") or "") or None
                    for work_rel in work.get("relations") or []:
                        wrt = str((work_rel or {}).get("type") or "").lower()
                        if wrt in ("composer", "writer", "lyricist"):
                            wtarget = (work_rel or {}).get("artist") or {}
                            if isinstance(wtarget, dict) and wtarget.get("name"):
                                writers.append(str(wtarget["name"]))
        except Exception as _wexc:
            logger.debug("Work-rels writer extraction failed", recording=mbid, error=str(_wexc))

        return {
            "title": recording.get("title"),
            "artist": rec_artist,
            "artist_mbid": rec_artist_mbid or None,
            "album": release.get("title") if release else None,
            "album_artist": (
                release.get("artist-credit", [{}])[0].get("name")
                if release and release.get("artist-credit")
                else None
            ),
            "isrc": _first_isrc(recording),
            "year": (
                int(release.get("date")[:4])
                if release and release.get("date")
                else None
            ),
            "recording_mbid": mbid,
            "confidence": confidence,
            "writer": ", ".join(dict.fromkeys(writers)) if writers else "",
            "work_mbid": work_mbid or "",
            # MusicBrainz genres from the recording (batch now requests
            # ``genres``) — lets track_stage fill musicbrainz_genres from the
            # album batch WITHOUT a per-track get_recording(genres+tags) call
            # (a 1 req/s MB call that timed out under contention and left the
            # genre columns empty → album/artist pages showed only Essentia +
            # Navidrome).
            "genres": [
                str(g.get("name") or "").strip()
                for g in (recording.get("genres") or [])
                if str(g.get("name") or "").strip()
            ] or [],
        }

    def lookup_album_metadata(
        self,
        entries: list[tuple[str, str]],
        candidates_per_entry: int = 5,
        album: str = "",
    ) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}
        album = str(album or "").strip()
        try:
            unique = sorted(
                {
                    (str(t or "").strip(), str(a or "").strip())
                    for t, a in (entries or [])
                    if t and a
                }
            )
            if not unique:
                return {}

            results: dict[str, dict[str, Any]] = {}
            for chunk_start in range(0, len(unique), _MB_BATCH_CHUNK):
                chunk = unique[chunk_start:chunk_start + _MB_BATCH_CHUNK]
                groups = [
                    (
                        f'(recording:"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}" '
                        f'AND artist:"{escape_lucene_special_chars(artist)}")'
                    )
                    for title, artist in chunk
                ]
                try:
                    recordings = self.http.search_recordings(
                        " OR ".join(groups),
                        limit=min(100, len(chunk) * candidates_per_entry),
                        # Request release/release-group data so
                        # ``_recording_matches_album`` can actually read
                        # ``recording["releases"]`` for album-anchor matching.
                        # Without this include, the field is absent from the
                        # search response and album anchoring silently never
                        # fires — matches fall back to pure title similarity.
                        # ``work-rels`` lets the batch carry per-recording
                        # WRITERS, avoiding a second per-track composer call.
                        # ``genres`` lets the batch carry per-recording MB
                        # genres, avoiding a per-track genre lookup.
                        inc="releases+work-rels+genres",
                    )
                except Exception as exc:
                    logger.debug("Album batch search failed", chunk_start=chunk_start, error=str(exc))
                    continue

                for title, artist in chunk:
                    norm_title = normalize_title_for_mbid_match(title)
                    best = None
                    best_score = 0.0
                    best_album_anchor = False
                    
                    for rec in recordings:
                        rec_title = str(rec.get("title") or "")
                        if not edition_annotations_compatible(title, rec_title):
                            continue
                            
                        sim = _mbid_similarity(
                            norm_title, normalize_title_for_mbid_match(rec_title)
                        )
                        if sim <= 0:
                            continue
                            
                        album_anchor = _recording_matches_album(rec, album)
                        if (
                            sim > best_score
                            or (
                                sim == best_score
                                and album_anchor
                                and not best_album_anchor
                            )
                        ):
                            best_score = sim
                            best = rec
                            best_album_anchor = album_anchor
                            
                    mbid = (best or {}).get("id", "")
                    
                    if not best or not mbid or best_score < _MB_BATCH_SIMILARITY_FLOOR:
                        continue
                        
                    confidence = round(best_score, 3)
                    results[self._cache_key(title, artist)] = self._recording_to_metadata(best, mbid, confidence)
                    
                    with self._mem_lock:
                        self._mbid_cache[self._cache_key(title, artist)] = (mbid, confidence)
                        
                has_items = False
                with self._mem_lock:
                    has_items = bool(self._mbid_cache)
                if has_items:
                    self._save_cache()
                    
            return results
        except Exception as exc:
            logger.debug("Album batch failed", entries_count=len(entries or []), error=str(exc))
            return {}

    def is_single(self, title: str, artist: str, album_track_count: int | None = None) -> bool:
        if not self.enabled or not title or not artist:
            return False
        try:
            for lookup_artist in _artist_lookup_candidates(artist):
                if self._recording_search_has_single_release(title, lookup_artist):
                    return True

                mbid, _confidence = self.get_suggested_mbid(title, lookup_artist)
                if mbid and self._recording_has_single_release(mbid, title=title):
                    return True

                if self._release_group_has_single_release(title, lookup_artist):
                    return True
            return False
        except Exception as exc:
            logger.debug("MusicBrainz is_single failed", artist=artist, track=title, error=str(exc))
            return False

    def _release_group_has_single_release(self, title: str, artist: str) -> bool:
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        rg_query = (
            f'releasegroup:"{escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        groups = self.http.search_release_groups(rg_query, limit=10)
        if not groups:
            groups = self.http.search_release_groups(
                f'artist:"{escape_lucene_special_chars(artist)}"',
                limit=50,
            )
        norm_title = normalize_title_for_lookup(title)
        for group in groups:
            pt = (
                group.get("primary-type")
                or group.get("primary_type")
                or group.get("type")
                or ""
            ).lower()
            if pt not in ("single", "ep"):
                continue
            if not edition_annotations_compatible(title, group.get("title") or ""):
                continue
            sim = _similarity(
                norm_title,
                normalize_title_for_lookup(group.get("title") or ""),
            )
            if sim >= 0.7:
                return True
        return False

    def _recording_search_has_single_release(self, title: str, artist: str) -> bool:
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = (
            f'recording:"{escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        for rec in self.http.search_recordings(query, limit=10):
            for release in rec.get("releases") or []:
                rg = release.get("release-group") or {}
                pt = (
                    rg.get("primary-type")
                    or rg.get("primary_type")
                    or rg.get("type")
                    or ""
                ).lower()
                if pt not in ("single", "ep"):
                    continue
                if self._rg_title_matches(title, rg.get("title") or ""):
                    return True
        return False

    def _rg_title_matches(self, title: str, rg_title: str) -> bool:
        if not title or not rg_title:
            return False
        if not edition_annotations_compatible(title, rg_title):
            return False
        norm_title = normalize_title_for_lookup(strip_single_release_suffix(title) or title)
        norm_rg = normalize_title_for_lookup(strip_single_release_suffix(rg_title) or rg_title)
        if norm_rg == norm_title:
            return True
        return _similarity(norm_rg, norm_title) >= 0.85

    def _recording_has_single_release(self, mbid: str, title: str = "") -> bool:
        recording = self.http.get_recording(
            mbid,
            inc="releases+release-groups",
            timeout=10.0,
        )
        if not recording:
            return False
        for release in recording.get("releases") or []:
            rg = release.get("release-group") or {}
            pt = (rg.get("primary-type") or rg.get("primary_type") or "").lower()
            rt = (rg.get("type") or "").lower()
            if pt not in ("single", "ep") and rt not in ("single", "ep"):
                continue
            if not title or self._rg_title_matches(title, rg.get("title") or ""):
                return True
        return False

    # -----------------------------------------------------------------------------
    # SIMPLE LOOKUPS
    # -----------------------------------------------------------------------------

    def get_artist_country(self, artist: str) -> str:
        if not self.enabled or not artist:
            return ""

        try:
            result = self.http.search_artists(
                f'artist:"{escape_lucene_special_chars(artist)}"',
                limit=1,
                inc="area",
            )

            if not result:
                return ""

            data = result[0]
            return (
                (data.get("area") or {}).get("name")
                or (data.get("begin-area") or {}).get("name")
                or ""
            )
        except Exception as exc:
            logger.debug("Artist country lookup failed", artist=artist, error=str(exc))
            return ""

    def get_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled:
            return []

        query = f'recording:"{escape_lucene_special_chars(strip_search_keywords(title))}" AND artist:"{escape_lucene_special_chars(artist)}"'

        try:
            recordings = self.http.search_recordings(query, limit=1, inc="tags+releases")

            if not recordings:
                return []

            tags = recordings[0].get("tags") or []
            return [t["name"] for t in tags if t.get("name")]

        except Exception as exc:
            logger.debug("Genre lookup failed", artist=artist, title=title, error=str(exc))
            return []

    # -----------------------------------------------------------------------------
    # MATCHING / RELEASE HELPERS
    # -----------------------------------------------------------------------------

    def search_releasegroup_matches(self, artist_name: str, album_name: str, limit: int = 10) -> list[dict[str, Any]]:
        if not artist_name or not album_name:
            return []

        clean_album = strip_search_keywords(album_name)
        query = f'artist:"{escape_lucene_special_chars(artist_name)}" AND releasegroup:"{escape_lucene_special_chars(clean_album)}"'

        try:
            groups = self.http.search_release_groups(query, limit=limit)
        except Exception as exc:
            logger.debug("Release-group search failed", artist=artist_name, album=album_name, error=str(exc))
            groups = []

        if not groups and clean_album:
            terms = normalize_title_for_lucene_query(clean_album)
            if terms:
                try:
                    groups = self.http.search_release_groups(
                        f'artist:"{escape_lucene_special_chars(artist_name)}" AND releasegroup:{terms}',
                        limit=limit,
                    )
                except Exception as exc:
                    logger.debug("Release-group fallback search failed", artist=artist_name, album=album_name, error=str(exc))
                    groups = []

        matches = []

        for group in groups:
            score = calculate_match_score(
                group.get("title") or "",
                group.get("artist-credit") or [],
                album_name,
                artist_name,
            )

            matches.append({
                "id": group.get("id"),
                "title": group.get("title"),
                "primary_type": group.get("primary-type"),
                "match_score": round(score, 3),
                "secondary_types": _parse_secondary_types(group.get("secondary-types")),
            })

        matches.sort(key=lambda x: x["match_score"], reverse=True)
        return matches

    # -----------------------------------------------------------------------------
    # MERGE HELPER
    # -----------------------------------------------------------------------------

    def merge_metadata(self, base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}

        def pick(*values: Any) -> Any:
            for v in values:
                if v:
                    return v
            return None

        return {
            "title": pick(overrides.get("title"), mb.get("title"), base.get("title")),
            "artist": pick(overrides.get("artist"), mb.get("artist"), base.get("artist")),
            "album": pick(overrides.get("album"), mb.get("album"), base.get("album")),
            "album_artist": pick(
                overrides.get("album_artist"),
                mb.get("album_artist"),
                base.get("album_artist"),
            ),
            "year": pick(overrides.get("year"), mb.get("year"), base.get("year")),
        }
    
    # ------------------------------------------------------------------
    # Relationship lookups (similar artists, collaborators)
    # ------------------------------------------------------------------

    def get_artist_relationships(self, artist_mbid: str, relation_type: str = "artist") -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []

        inc_map = {
            "artist": "artist-rels",
            "recording": "recording-rels",
            "work": "work-rels",
        }
        inc = inc_map.get(relation_type, "artist-rels")

        try:
            data = self.http.get_artist(artist_mbid, inc=inc)
            return data.get("relations", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch relationships for artist", artist_mbid=artist_mbid, error=str(exc))
            return []

    def get_recording_relationships(self, recording_mbid: str) -> list[dict[str, Any]]:
        if not self.enabled or not recording_mbid:
            return []

        try:
            data = self.http.get_recording(
                recording_mbid,
                inc="artist-rels+work-rels+work-level-rels+recording-level-rels",
            )
            return data.get("relations", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch recording relationships", recording_mbid=recording_mbid, error=str(exc))
            return []

    def get_composers_for_recording(self, recording_mbid: str) -> list[str]:
        if not self.enabled or not recording_mbid:
            return []
        composers: list[str] = []
        for rel in self.get_recording_relationships(recording_mbid):
            rel_type = str(rel.get("type") or "").lower()
            if rel_type in ("composer", "writer", "lyricist"):
                target = rel.get("artist") or {}
                if target and target.get("name"):
                    composers.append(target["name"])
            work = rel.get("work") or {}
            for work_rel in work.get("relations") or []:
                work_rel_type = str(work_rel.get("type") or "").lower()
                if work_rel_type in ("composer", "writer", "lyricist"):
                    work_target = work_rel.get("artist") or {}
                    if work_target and work_target.get("name"):
                        composers.append(work_target["name"])
        return list(dict.fromkeys(composers))

    # ------------------------------------------------------------------
    # Genre-enriched lookups
    # ------------------------------------------------------------------

    def get_recording_genres(self, title: str, artist: str) -> list[str]:
        from api_clients.musicbrainz_http import escape_lucene_special_chars

        if not self.enabled or not title or not artist:
            return []

        query = f'recording:"{escape_lucene_special_chars(title)}" AND artist:"{escape_lucene_special_chars(artist)}"'

        try:
            recordings = self.http.search_recordings_with_genres(query, limit=3)
            if not recordings:
                return []
            genres = []
            for rec in recordings:
                for g in (rec.get("genres") or []):
                    name = g.get("name") if isinstance(g, dict) else str(g)
                    if name and name not in genres:
                        genres.append(name)
            return genres
        except Exception as exc:
            logger.debug("Failed to fetch genres", artist=artist, track=title, error=str(exc))
            return []


_service = None
_shared_mb_service: "MusicBrainzService | None" = None

def _get_service() -> MusicBrainzService:
    global _service
    if _service is None:
        with _INIT_LOCK:
            if _service is None:
                _service = MusicBrainzService()
    return _service


def get_shared_mb_service() -> "MusicBrainzService":
    global _shared_mb_service
    if _shared_mb_service is None:
        with _INIT_LOCK:
            if _shared_mb_service is None:
                _shared_mb_service = MusicBrainzService(http_client=get_shared_mb_client())
    return _shared_mb_service


def lookup_recording_metadata(title: str, artist: str) -> dict[str, Any]:
    return _get_service().lookup_recording_metadata(title, artist)


def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return _get_service().merge_metadata(base, mb, overrides)

def fetch_musicbrainz_release_metadata(release_id: str) -> dict[str, Any] | None:
    try:
        data = get_shared_mb_client().get_release(
            release_id,
            inc="recordings+artist-credits+release-groups+work-rels+genres",
        )

        if not data:
            logger.debug("Release not found", release_id=release_id)
            return None

        rg = data.get("release-group", {})

        release_year = (
            (rg.get("first-release-date") or "")[:4]
            or (data.get("date") or "")[:4]
        )

        # ── Release-level metadata (the user's master checklist) ─────────
        # secondary-types drives the ``compilation`` flag ("Various Artists"
        # soundtracks / compilations stay in one entry); the ORIGINAL release
        # date comes from the release-group's first-release-date so remasters
        # sort by when the album FIRST debuted, not the reissue date.
        _secondary_types = _parse_secondary_types(rg.get("secondary-types"))
        release_info: dict[str, Any] = {
            "release_title": data.get("title"),
            "release_year": release_year,
            "artist": "",
            "disc_count": len(data.get("media", [])),
            "tracks": [],
            "release_mbid": data.get("id"),
            "release_group_mbid": rg.get("id") or "",
            "compilation": 1 if "compilation" in _secondary_types else 0,
            "original_date": rg.get("first-release-date") or data.get("date") or "",
            "original_year": (
                (rg.get("first-release-date") or data.get("date") or "")[:4]
            ),
        }

        if data.get("artist-credit"):
            release_info["artist"] = primary_album_artist(data["artist-credit"])
            release_info["artist_credit"] = build_artist_credit_string(data["artist-credit"])
            # Album-artist MBID from the PRIMARY credit — links aliases and
            # featured collaborations back to the primary band's page.
            _first_credit = (data.get("artist-credit") or [{}])[0] or {}
            _first_artist = _first_credit.get("artist") or {}
            if isinstance(_first_artist, dict):
                release_info["album_artist_mbid"] = str(_first_artist.get("id") or "")

        # Absolute sequential track number across all discs (handles local
        # libraries numbered 1..22 across two discs, or tagged per-disc).
        _absolute_track_number = 1

        for disc_index, media in enumerate(data.get("media", []), start=1):
            for track in media.get("tracks", []):
                recording = track.get("recording", {})

                track_info: dict[str, Any] = {
                    "disc_number": disc_index,
                    "track_number": track.get("position"),
                    # The absolute sequential number (medium position offset
                    # by all previous discs) — lets locally-numbered 1..22
                    # libraries match multi-disc releases without disc tags.
                    "absolute_track_number": _absolute_track_number,
                    "title": track.get("title") or recording.get("title"),
                    "recording_mbid": recording.get("id"),
                    "duration": track.get("length"),
                    "artist": (
                        build_artist_credit_string(recording.get("artist-credit"))
                        if recording.get("artist-credit")
                        else release_info.get("artist_credit") or release_info.get("artist")
                    ),
                }
                _absolute_track_number += 1

                # ── Recording work relationships (writers + covers) ──────
                # The release lookup now requests ``work-rels``, so each
                # recording carries its work relations.  A "recording of"
                # work relation means the recording IS a performance of that
                # work; the work's own relations name the WRITERS.  A track
                # whose work is by a different artist is a COVER.
                writers: list[str] = []
                composers: list[str] = []
                lyricists: list[str] = []
                work_mbid: str | None = None
                work_title: str | None = None
                work_artist: str | None = None
                work_iswc: str | None = None
                is_cover = False
                try:
                    for rel in recording.get("relations") or []:
                        rel_type = str(rel.get("type") or "").lower()
                        work = rel.get("work") or {}
                        if rel_type in ("performance", "recording of") and work:
                            work_mbid = work.get("id")
                            work_title = work.get("title")
                            work_iswc = str(work.get("iswc") or "").strip() or None
                            # The work's composer/writer relations.
                            for work_rel in work.get("relations") or []:
                                wrt = str(work_rel.get("type") or "").lower()
                                wtarget = work_rel.get("artist") or {}
                                if wrt == "composer" and wtarget.get("name"):
                                    composers.append(str(wtarget["name"]))
                                elif wrt in ("writer", "lyricist") and wtarget.get("name"):
                                    lyricists.append(str(wtarget["name"]))
                                if wrt in ("composer", "writer", "lyricist") and wtarget.get("name"):
                                    writers.append(str(wtarget["name"]))
                            # The work's artist credit — a cover when it
                            # differs from the recording/release artist.
                            work_credit = work.get("artist-credit") or []
                            if work_credit:
                                work_artist = primary_album_artist(work_credit)
                    if composers:
                        track_info["composer"] = ", ".join(dict.fromkeys(composers))
                    if lyricists:
                        track_info["lyricist"] = ", ".join(dict.fromkeys(lyricists))
                    if writers:
                        track_info["writer"] = ", ".join(dict.fromkeys(writers))
                    if work_mbid:
                        track_info["work_mbid"] = work_mbid
                    if work_title:
                        track_info["work_title"] = work_title
                    if work_iswc:
                        track_info["iswc"] = work_iswc
                    if work_artist:
                        track_info["work_artist"] = work_artist
                        release_artist_norm = _normalise_artist_key(
                            track_info.get("artist") or release_info.get("artist") or ""
                        )
                        work_artist_norm = _normalise_artist_key(work_artist)
                        if (
                            release_artist_norm
                            and work_artist_norm
                            and release_artist_norm != work_artist_norm
                        ):
                            is_cover = True
                    if is_cover:
                        track_info["is_cover"] = True
                        track_info["original_cover_artist"] = work_artist
                        # originaltitle = the WORK title (the song being
                        # covered), not the local cover's title.
                        if work_title:
                            track_info["original_title"] = work_title
                except Exception as _rel_exc:
                    logger.debug("Work-relation parse failed", recording=recording.get("id"), error=str(_rel_exc))

                # ── Recording genres from MusicBrainz ────────────────────
                try:
                    mb_genres = [
                        str(g.get("name") or "").strip()
                        for g in (recording.get("genres") or [])
                        if str(g.get("name") or "").strip()
                    ]
                    if mb_genres:
                        track_info["musicbrainz_genres"] = ", ".join(dict.fromkeys(mb_genres))
                except Exception:
                    pass

                release_info["tracks"].append(track_info)

        try:
            from api_clients.coverartarchive import get_release_front_image_bytes
            cover = get_release_front_image_bytes(release_id)
            if cover:
                release_info["cover_art"] = cover
        except Exception:
            pass

        return release_info

    except Exception as e:
        logger.error("MB RELEASE metadata error", error=str(e), exc_info=True)
        return None


def _normalise_artist_key(value: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for artist compare."""
    import re as _re
    return _re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def resolve_release_id(release_id: str) -> str:
    if not release_id:
        return release_id

    try:
        http = _get_service().http
        data = http.get_release(release_id, inc="")
        if data and data.get("id"):
            return release_id
    except Exception:
        pass

    try:
        releases = http.browse_releases_for_group(release_id, inc="media", limit=50)
        if releases:
            def _total_tracks(rel: dict[str, Any]) -> int:
                return sum(
                    int((m.get("track-count") or 0))
                    for m in (rel.get("media") or [])
                )

            official = [
                r for r in releases
                if str(r.get("status") or "").strip().lower() == "official"
            ]
            candidates = [r for r in (official or releases) if _total_tracks(r) > 0]
            candidates = candidates or (official or releases)
            best = max(candidates, key=_total_tracks)
            resolved = best["id"]
            logger.info(
                "Release-group resolved to release",
                release_group_mbid=release_id, resolved_id=resolved, tracks=_total_tracks(best), status=best.get("status")
            )
            return resolved
    except Exception as exc:
        logger.warning("Failed to resolve release-group", release_id=release_id, error=str(exc))

    return release_id


def fetch_release_metadata(release_id: str) -> dict[str, Any] | None:
    try:
        service = _get_service()
        http = service.http

        data = http.get_release(
            release_id,
            inc="recordings+artist-credits+release-groups"
        )

        if not data:
            return None

        rg = data.get("release-group", {})

        release_year = (
            (rg.get("first-release-date") or "")[:4]
            or (data.get("date") or "")[:4]
        )

        release_info: dict[str, Any] = {
            "release_title": data.get("title"),
            "release_year": release_year,
            "artist": "",
            "disc_count": len(data.get("media", [])),
            "tracks": [],
            "release_mbid": data.get("id"),
        }

        if data.get("artist-credit"):
            release_info["artist"] = primary_album_artist(data["artist-credit"])
            release_info["artist_credit"] = build_artist_credit_string(data["artist-credit"])

        for disc_index, media in enumerate(data.get("media", []), start=1):
            for track in media.get("tracks", []):
                recording = track.get("recording", {})

                release_info["tracks"].append({
                    "disc_number": disc_index,
                    "track_number": track.get("position"),
                    "title": track.get("title") or recording.get("title"),
                    "recording_mbid": recording.get("id"),
                    "duration": track.get("length"),
                    "artist": (
                        build_artist_credit_string(recording.get("artist-credit"))
                        if recording.get("artist-credit")
                        else release_info.get("artist_credit") or release_info.get("artist")
                    ),
                })

        return release_info

    except Exception as e:
        logger.error("MB RELEASE track fetch error", error=str(e), exc_info=True)
        return None


def _mb_artist_credit_name(artist_credit: list[Any] | str) -> str:
    if isinstance(artist_credit, list) and artist_credit:
        first = artist_credit[0]
        if isinstance(first, dict):
            return str(first.get("name") or "")
    elif isinstance(artist_credit, str):
        return artist_credit
    return ""


def _cover_art_url(rg_id: str, release_id: str = "") -> str:
    if rg_id:
        return f"https://coverartarchive.org/release-group/{rg_id}/front-250"
    if release_id:
        return f"https://coverartarchive.org/release/{release_id}/front-250"
    return ""


def _lookup_existing_mbid(existing_mbid: str, artist: str, album: str) -> dict[str, Any] | None:
    if not existing_mbid:
        return None
    client = get_shared_mb_client()

    try:
        rel_data = client.get_release(existing_mbid, inc="artist-credits+release-groups")
        if rel_data:
            rel_artist = primary_album_artist(rel_data.get("artist-credit") or []) or artist
            rg = rel_data.get("release-group") or {}
            rg_id = rg.get("id", "")
            primary_type = rg.get("primary-type", "Album")
            secondary_types = _parse_secondary_types(rg.get("secondary-types"))
            display_date = rg.get("first-release-date", "") or rel_data.get("date", "")
            return {
                "mbid": existing_mbid,
                "title": rel_data.get("title", album),
                "artist": rel_artist,
                "primary_type": primary_type,
                "secondary_types": secondary_types,
                "first_release_date": display_date,
                "cover_art_url": _cover_art_url(rg_id, existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release",
            }
    except Exception as exc:
        logger.debug("Stored release lookup failed", existing_mbid=existing_mbid, error=str(exc))

    try:
        rg_data = client.get_release_group(existing_mbid, inc="artist-credits")
        if rg_data:
            rg_artist = _mb_artist_credit_name(rg_data.get("artist-credit") or []) or artist
            return {
                "mbid": existing_mbid,
                "title": rg_data.get("title", album),
                "artist": rg_artist,
                "primary_type": rg_data.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(rg_data.get("secondary-types")),
                "first_release_date": rg_data.get("first-release-date", ""),
                "cover_art_url": _cover_art_url(existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release-group",
            }
    except Exception as exc:
        logger.debug("Stored release-group lookup failed", existing_mbid=existing_mbid, error=str(exc))
    return None


def lookup_musicbrainz_album(artist: str, album: str, existing_mbid: str = "") -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    if existing_mbid:
        stored = _lookup_existing_mbid(existing_mbid, artist, album)
        if stored:
            results.append(stored)
            logger.info("Found stored MBID",
                        mbid_type=stored["mbid_type"], mbid=existing_mbid, title=stored["title"], artist=stored["artist"])

    query = f'release:"{escape_lucene_special_chars(album)}" AND artist:"{escape_lucene_special_chars(artist)}"'
    try:
        groups = get_shared_mb_client().search_release_groups(query, limit=10)
    except Exception as exc:
        logger.warning("MusicBrainz album search unavailable", error=str(exc))
        groups = []

    seen_mbids = {r["mbid"] for r in results}
    for rg in groups or []:
        rg_id = rg.get("id", "")
        if not rg_id or rg_id in seen_mbids:
            continue
        rg_title = rg.get("title", "")
        primary_type = rg.get("primary-type", "Album")
        secondary_types = _parse_secondary_types(rg.get("secondary-types"))
        first_release = rg.get("first-release-date", "")
        rg_artist = _mb_artist_credit_name(rg.get("artist-credit") or [])
        confidence = calculate_match_score(rg_title, rg.get("artist-credit") or [], album, artist)
        results.append({
            "mbid": rg_id,
            "title": rg_title,
            "artist": rg_artist,
            "primary_type": primary_type,
            "secondary_types": secondary_types,
            "first_release_date": first_release,
            "cover_art_url": _cover_art_url(rg_id),
            "confidence": round(confidence, 3),
            "source": "musicbrainz",
            "is_stored_mbid": False,
            "mbid_type": "release-group",
        })
        seen_mbids.add(rg_id)

    stored = [r for r in results if r.get("is_stored_mbid")]
    others = sorted(
        [r for r in results if not r.get("is_stored_mbid")],
        key=lambda r: r.get("confidence") or 0.0,
        reverse=True,
    )
    return {"results": (stored + others)[:11]}


def get_release_group_releases(rg_mbid: str, include_track_counts: bool = False) -> dict[str, Any]:
    try:
        data = get_shared_mb_client().get_release_group(rg_mbid, inc="releases")
        if not data:
            return {"success": False, "error": "No release-group data returned"}
        raw_releases = data.get("releases", []) or []

        releases: list[dict[str, Any]] = []
        for r in raw_releases:
            media = r.get("media") or []
            total_tracks = sum(int(m.get("track-count", 0) or 0) for m in media)
            formats = list({
                str(m.get("format") or "").strip()
                for m in media if m.get("format")
            })
            releases.append({
                "id": r.get("id", ""),
                "title": r.get("title", ""),
                "date": r.get("date", ""),
                "country": r.get("country", ""),
                "status": r.get("status", ""),
                "disambiguation": r.get("disambiguation", ""),
                "track_count": total_tracks,
                "disc_count": len(media),
                "formats": [f for f in formats if f],
                "cover_art_url": (
                    f"https://coverartarchive.org/release/{r.get('id')}/front-250"
                    if r.get("id") else ""
                ),
            })

        if include_track_counts and releases:
            _enrich_releases_with_track_counts(releases, rg_mbid=rg_mbid)

        return {"success": True, "releases": releases}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _enrich_releases_with_track_counts(releases: list[dict[str, Any]], rg_mbid: str | None = None) -> None:
    if not releases:
        return
    if not rg_mbid:
        first_rg = releases[0].get("release-group")
        if isinstance(first_rg, dict):
            rg_mbid = first_rg.get("id")
    if not rg_mbid:
        return

    try:
        browse_releases = get_shared_mb_client().browse_releases_for_group(
            rg_mbid, inc="media", limit=100,
        )
        tc_lookup: dict[str, int] = {}
        for rel in browse_releases:
            rel_id = rel.get("id")
            if not rel_id:
                continue
            total = 0
            for medium in rel.get("media", []):
                total += int(medium.get("track-count", 0) or 0)
            if total > 0:
                tc_lookup[rel_id] = total

        for rel in releases:
            rel_id = rel.get("id")
            if rel_id and rel_id in tc_lookup:
                rel["track_count"] = tc_lookup[rel_id]
    except Exception as exc:
        logger.debug("Failed to fetch track counts", release_group_mbid=rg_mbid, error=str(exc))


def compare_musicbrainz_release(artist: str, album: str, rg_mbid: str) -> dict[str, Any]:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        import difflib as _difflib

        _direct = None
        try:
            _direct = get_shared_mb_client().get_release(rg_mbid, inc="")
        except Exception:
            _direct = None

        if _direct and _direct.get("id"):
            release_id = rg_mbid
        else:
            best = get_musicbrainz_best_release(artist, album, rg_mbid)
            best_release = (best or {}).get("best_release")
            release_id = (best_release or {}).get("id") or rg_mbid
            
            if not (best_release or {}).get("id"):
                resolved = resolve_release_id(rg_mbid)
                if resolved and resolved != rg_mbid:
                    release_id = resolved

        mb_release = fetch_musicbrainz_release_metadata(release_id)
        if not mb_release:
            return {"success": False, "error": "Could not fetch MusicBrainz release data"}

        mb_tracks = mb_release.get("tracks", [])
        mb_year = str(mb_release.get("release_year") or "")
        mb_release_title = str(mb_release.get("release_title") or "")

        library_tracks: list[dict[str, Any]] = []
        try:
            with db_session() as session:
                result = session.execute(
                    text(_COMPARE_LIBRARY_TRACKS_SQL),
                    {"artist": artist, "album": album},
                )
                library_tracks = [dict(r._mapping) for r in result.fetchall()]
        except Exception as exc:
            logger.debug("Library track fetch failed", error=str(exc))

        def _disc_track_key(t: dict[str, Any]) -> tuple[int, int]:
            def _num(v: Any, default: int) -> int:
                s = str(v or "").strip()
                if not s:
                    return default
                try:
                    return int(s.split("/")[0].strip())
                except (TypeError, ValueError):
                    return default
            return (_num(t.get("disc_number"), 1), _num(t.get("track_number"), 999))

        library_tracks.sort(key=_disc_track_key)

        if not library_tracks:
            return {
                "success": False,
                "error": "No library tracks found for this album",
                "comparison": [],
            }

        def _norm(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "").lower().strip())

        def _core(value: str) -> str:
            return re.sub(r"\s*[\(\[].+$", "", _norm(value)).strip()

        lib_by_tracknum: dict[tuple[int, int], dict[str, Any]] = {}
        lib_by_title: dict[tuple[int, str], dict[str, Any]] = {}
        
        for t in library_tracks:
            disc = int(t.get("disc_number") or 1)
            tn = t.get("track_number")
            if tn is not None:
                try:
                    lib_by_tracknum[(disc, int(str(tn).split("/")[0].strip()))] = t
                except (TypeError, ValueError):
                    pass
            lib_by_title[(disc, _norm(t.get("title") or ""))] = t

        matched_lib_ids: set[Any] = set()
        comparison: list[dict[str, Any]] = []

        _DURATION_TOLERANCE_SEC = 5.0
        _TRACK_NUM_TITLE_SIM_MIN = 0.30

        for mb_track in mb_tracks:
            disc = int(mb_track.get("disc_number") or 1)
            mb_num_raw = mb_track.get("track_number")
            try:
                mb_num = int(str(mb_num_raw).split("/")[0].strip()) if mb_num_raw is not None else None
            except (TypeError, ValueError):
                mb_num = None
            mb_title = str(mb_track.get("title") or "")
            norm_mb = _norm(mb_title)
            norm_mb_core = _core(mb_title)
            mb_recording_id = str(mb_track.get("recording_mbid") or "")
            mb_duration_ms = mb_track.get("duration")
            mb_duration_sec = (int(mb_duration_ms) / 1000.0) if mb_duration_ms else None

            lib_track = None

            if mb_num is not None and not lib_track:
                candidate = lib_by_tracknum.get((disc, mb_num))
                if candidate is not None:
                    if not norm_mb or _difflib.SequenceMatcher(
                        None, norm_mb, _norm(candidate.get("title") or "")
                    ).ratio() >= _TRACK_NUM_TITLE_SIM_MIN:
                        lib_track = candidate

            if lib_track is None:
                lib_track = lib_by_title.get((disc, norm_mb))

            if lib_track is None:
                best_ratio, best_t = 0.0, None
                for t in library_tracks:
                    if int(t.get("disc_number") or 1) != disc:
                        continue
                    if t.get("id") in matched_lib_ids:
                        continue
                    ratio = _difflib.SequenceMatcher(None, norm_mb, _norm(t.get("title") or "")).ratio()
                    if ratio > best_ratio and ratio >= 0.80:
                        best_ratio, best_t = ratio, t
                lib_track = best_t

            if lib_track is None and norm_mb_core and norm_mb_core != norm_mb:
                candidate = lib_by_title.get((disc, norm_mb_core))
                if candidate is not None and candidate.get("id") not in matched_lib_ids:
                    lib_track = candidate
                if lib_track is None:
                    best_ratio, best_t = 0.0, None
                    for t in library_tracks:
                        if int(t.get("disc_number") or 1) != disc:
                            continue
                        if t.get("id") in matched_lib_ids:
                            continue
                        ratio = _difflib.SequenceMatcher(None, norm_mb_core, _norm(t.get("title") or "")).ratio()
                        if ratio > best_ratio and ratio >= 0.80:
                            best_ratio, best_t = ratio, t
                    lib_track = best_t

            entry: dict[str, Any] = {
                "mb_track_number": mb_num,
                "mb_disc_number": disc,
                "mb_title": mb_title,
                "mb_artist": "",
                "mb_recording_id": mb_recording_id,
                "mb_year": mb_year,
                "mb_duration": None,
                "mb_duration_sec": int(mb_duration_sec) if mb_duration_sec else None,
                # Full MusicBrainz enrichment for this recording — carried so
                # "Update All Tracks" can apply composer / writer / genres /
                # cover / work MBID to the local track + file tags (the
                # reported gap: only title/track/year/mbid were applied).
                "mb_writer": mb_track.get("writer") or "",
                "mb_work_mbid": mb_track.get("work_mbid") or "",
                "mb_work_title": mb_track.get("work_title") or "",
                "mb_work_artist": mb_track.get("work_artist") or "",
                "mb_is_cover": bool(mb_track.get("is_cover")),
                "mb_original_cover_artist": mb_track.get("original_cover_artist") or "",
                "mb_musicbrainz_genres": mb_track.get("musicbrainz_genres") or "",
                "mb_artist_credit": mb_track.get("artist") or "",
                "library_track_id": None,
                "library_title": None,
                "library_track_number": None,
                "library_disc_number": None,
                "library_artist": None,
                "library_year": None,
                "library_duration": None,
                "matched": False,
                "needs_update": False,
                "diff_fields": [],
            }

            if lib_track is not None:
                matched_lib_ids.add(lib_track["id"])
                entry.update({
                    "matched": True,
                    "library_track_id": lib_track.get("id"),
                    "library_title": lib_track.get("title", ""),
                    "library_track_number": lib_track.get("track_number"),
                    "library_disc_number": int(lib_track.get("disc_number") or 1),
                    "library_artist": lib_track.get("artist", ""),
                    "library_year": str(lib_track.get("year") or ""),
                })
                raw_lib_dur = lib_track.get("duration")
                lib_duration_sec = None
                if raw_lib_dur not in (None, "", 0, "0"):
                    try:
                        val = float(raw_lib_dur)
                        lib_duration_sec = (val / 1000.0) if val > 10000 else val
                        lib_duration_sec = lib_duration_sec if lib_duration_sec > 0 else None
                    except (TypeError, ValueError):
                        lib_duration_sec = None
                entry["library_duration"] = lib_duration_sec

                def _fmt_dur(sec: float | None) -> str | None:
                    if sec is None:
                        return None
                    s = int(round(sec))
                    return f"{s // 60}:{s % 60:02d}"

                entry["mb_duration"] = _fmt_dur(mb_duration_sec)
                entry["library_duration"] = _fmt_dur(lib_duration_sec)

                diff_fields: list[str] = []
                lib_title = str(lib_track.get("title") or "")
                if mb_title and mb_title != lib_title:
                    cover_match = re.search(r'\([^)]*\bcover\b[^)]*\)', lib_title, re.IGNORECASE)
                    if cover_match:
                        stripped = re.sub(r'\s*\([^)]*\bcover\b[^)]*\)', '', lib_title, flags=re.IGNORECASE).strip()
                        if stripped.lower() != mb_title.lower():
                            diff_fields.append("title")
                    else:
                        diff_fields.append("title")
                
                lib_tn = lib_track.get("track_number")
                if mb_num is not None and str(mb_num) != str(lib_tn or ""):
                    diff_fields.append("track_number")
                
                lib_year = str(lib_track.get("year") or "")
                if mb_year and mb_year != lib_year:
                    diff_fields.append("year")
                
                lib_mbid = str(lib_track.get("mbid") or "").strip()
                if mb_recording_id and not lib_mbid:
                    diff_fields.append("mbid")
                
                if mb_duration_sec is not None and lib_duration_sec is not None:
                    if abs(mb_duration_sec - lib_duration_sec) > _DURATION_TOLERANCE_SEC:
                        diff_fields.append("duration")
                
                if int(lib_track.get("disc_number") or 1) != disc:
                    diff_fields.append("disc_number")

                import json as _json_cmp
                try:
                    ignored = set(_json_cmp.loads(lib_track.get("mb_ignored_fields") or "[]"))
                except Exception:
                    ignored = set()
                diff_fields = [f for f in diff_fields if f not in ignored]
                entry["diff_fields"] = diff_fields
                entry["needs_update"] = len(diff_fields) > 0

            comparison.append(entry)

        matched_mb_recording_ids = {e.get("mb_recording_id", "") for e in comparison if e.get("matched")}
        
        for mb_track in mb_tracks:
            mb_recording_id = str(mb_track.get("recording_mbid") or "")
            if mb_recording_id and mb_recording_id in matched_mb_recording_ids:
                continue
            disc = int(mb_track.get("disc_number") or 1)
            mb_title = str(mb_track.get("title") or "")
            norm_mb = _norm(mb_title)
            norm_mb_core = _core(mb_title)
            mb_num_raw = mb_track.get("track_number")
            
            try:
                mb_num = int(str(mb_num_raw).split("/")[0].strip()) if mb_num_raw is not None else None
            except (TypeError, ValueError):
                mb_num = None

            already_matched = any(
                e.get("matched") and e.get("mb_title") == mb_title
                and int(e.get("mb_disc_number") or 1) == disc
                and e.get("mb_track_number") == mb_num
                for e in comparison
            )
            if already_matched:
                continue

            best_ratio, best_lib = 0.0, None
            for t in library_tracks:
                if t.get("id") in matched_lib_ids:
                    continue
                lib_disc = int(t.get("disc_number") or 1)
                if lib_disc == disc:
                    continue  
                ratio = _difflib.SequenceMatcher(None, norm_mb, _norm(t.get("title") or "")).ratio()
                if ratio < 0.80 and norm_mb_core and norm_mb_core != norm_mb:
                    ratio = max(ratio, _difflib.SequenceMatcher(None, norm_mb_core, _norm(t.get("title") or "")).ratio())
                if ratio > best_ratio and ratio >= 0.80:
                    best_ratio, best_lib = ratio, t

            if best_lib is None:
                continue

            matched_lib_ids.add(best_lib["id"])
            if mb_recording_id:
                matched_mb_recording_ids.add(mb_recording_id)

            mb_duration_ms = mb_track.get("duration")
            mb_duration_sec = (int(mb_duration_ms) / 1000.0) if mb_duration_ms else None

            entry = {
                "mb_track_number": mb_num,
                "mb_disc_number": disc,
                "mb_title": mb_title,
                "mb_artist": "",
                "mb_recording_id": mb_recording_id,
                "mb_year": mb_year,
                "mb_duration": None,
                "mb_duration_sec": int(mb_duration_sec) if mb_duration_sec else None,
                # Full MusicBrainz enrichment for this recording (see the
                # matched entry above).
                "mb_writer": mb_track.get("writer") or "",
                "mb_work_mbid": mb_track.get("work_mbid") or "",
                "mb_work_title": mb_track.get("work_title") or "",
                "mb_work_artist": mb_track.get("work_artist") or "",
                "mb_is_cover": bool(mb_track.get("is_cover")),
                "mb_original_cover_artist": mb_track.get("original_cover_artist") or "",
                "mb_musicbrainz_genres": mb_track.get("musicbrainz_genres") or "",
                "mb_artist_credit": mb_track.get("artist") or "",
                "library_track_id": best_lib["id"],
                "library_title": best_lib.get("title", ""),
                "library_track_number": best_lib.get("track_number"),
                "library_disc_number": int(best_lib.get("disc_number") or 1),
                "library_artist": best_lib.get("artist", ""),
                "library_year": str(best_lib.get("year") or ""),
                "library_duration": None,
                "matched": True,
                "cross_disc_match": True,
                "needs_update": False,
                "diff_fields": [],
            }

            diff_fields = []
            lib_title = str(best_lib.get("title") or "")
            if mb_title and mb_title != lib_title:
                cover_match = re.search(r'\([^)]*\bcover\b[^)]*\)', lib_title, re.IGNORECASE)
                if cover_match:
                    stripped = re.sub(r'\s*\([^)]*\bcover\b[^)]*\)', '', lib_title, flags=re.IGNORECASE).strip()
                    if stripped.lower() != mb_title.lower():
                        diff_fields.append("title")
                else:
                    diff_fields.append("title")
                    
            if mb_num is not None and str(mb_num) != str(best_lib.get("track_number") or ""):
                diff_fields.append("track_number")
                
            lib_year = str(best_lib.get("year") or "")
            if mb_year and mb_year != lib_year:
                diff_fields.append("year")
                
            lib_mbid = str(best_lib.get("mbid") or "").strip()
            if mb_recording_id and not lib_mbid:
                diff_fields.append("mbid")
                
            diff_fields.append("disc_number")

            import json as _json_xdisc
            try:
                ignored = set(_json_xdisc.loads(best_lib.get("mb_ignored_fields") or "[]"))
            except Exception:
                ignored = set()
                
            diff_fields = list(dict.fromkeys(f for f in diff_fields if f not in ignored))
            entry["diff_fields"] = diff_fields
            entry["needs_update"] = len(diff_fields) > 0
            comparison.append(entry)

        tracks_needing_update = sum(1 for c in comparison if c.get("needs_update"))

        extra_tracks = []
        for t in library_tracks:
            if t["id"] not in matched_lib_ids:
                extra_tracks.append({
                    "library_track_id": t["id"],
                    "library_title": t.get("title", ""),
                    "library_track_number": t.get("track_number"),
                    "library_disc_number": int(t.get("disc_number") or 1),
                    "library_artist": t.get("artist", ""),
                })

        return {
            "success": True,
            "mb_title": mb_release_title,
            "mb_year": mb_year,
            "mb_artist": str(mb_release.get("artist") or ""),
            "mb_release_mbid": str(mb_release.get("release_mbid") or release_id or ""),
            "mb_release_group_mbid": rg_mbid,
            "release_group_mbid": rg_mbid,
            "release_mbid": release_id,
            "mb_album_artist_mbid": str(mb_release.get("album_artist_mbid") or ""),
            "mb_disc_count": int(mb_release.get("disc_count") or 0),
            "mb_artist_credit": str(mb_release.get("artist_credit") or ""),
            "comparison": comparison,
            "extra_tracks": extra_tracks,
            "tracks_needing_update": tracks_needing_update,
            "total_tracks": len(comparison),
        }
    except Exception as exc:
        logger.error("Compare MusicBrainz release error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}


def _get_local_track_count(artist: str, album: str) -> int:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            result = session.execute(
                text("SELECT COUNT(*) AS cnt FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) AND LOWER(COALESCE(album, '')) = LOWER(:album)"),
                {"artist": artist, "album": album},
            )
            row = result.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def get_musicbrainz_best_release(artist: str, album: str, rg_mbid: str) -> dict[str, Any]:
    try:
        client = get_shared_mb_client()

        releases_raw = client.browse_releases_for_group(
            rg_mbid, inc="media+labels", limit=50,
        )
        releases: list[dict[str, Any]] = []
        
        for r in releases_raw or []:
            media = r.get("media") or []
            total_tracks = sum(int(m.get("track-count", 0) or 0) for m in media)
            disc_count = len(media)
            formats = list({str(m.get("format") or "").strip() for m in media if m.get("format")})
            releases.append({
                "id": r.get("id", ""),
                "title": r.get("title", ""),
                "date": r.get("date", ""),
                "country": r.get("country", ""),
                "status": r.get("status", ""),
                "disambiguation": r.get("disambiguation", ""),
                "track_count": total_tracks,
                "disc_count": disc_count,
                "formats": [f for f in formats if f],
                "cover_art_url": (
                    f"https://coverartarchive.org/release/{r.get('id')}/front-250"
                    if r.get("id") else ""
                ),
            })

        releases.sort(key=lambda x: (x.get("date") == "", x.get("date") or ""))

        if not releases:
            return {
                "success": True,
                "releases": [],
                "best_release": None,
                "confidence": 0,
                "local_track_count": None,
            }

        local_tc = _get_local_track_count(artist, album)
        local_tc = local_tc if local_tc > 0 else None

        def _score_release(rel: dict[str, Any]) -> float:
            score = 0.0
            if local_tc is not None:
                diff = abs(local_tc - int(rel.get("track_count") or 0))
                score -= diff * 100.0
                
            if (rel.get("status") or "").lower() == "official":
                score += 50.0
                
            date = (rel.get("date") or "").strip()
            if date and date[:4].isdigit():
                score += max(0.0, 2100.0 - int(date[:4])) * 0.01
                
            if album and rel.get("title"):
                score += _similarity(album.lower(), str(rel["title"]).lower()) * 30.0
            return score

        scored = sorted(
            ((rel, _score_release(rel)) for rel in releases),
            key=lambda x: x[1],
            reverse=True,
        )
        best_release, best_score = scored[0][0], scored[0][1]

        confidence = 0.0
        if local_tc is not None:
            if int(best_release.get("track_count") or 0) == local_tc:
                confidence = 1.0
            else:
                diff = abs(local_tc - int(best_release.get("track_count") or 0))
                confidence = max(0.0, 1.0 - (diff * 0.2))
        else:
            confidence = 0.5 

        return {
            "success": True,
            "releases": releases,
            "best_release": best_release,
            "confidence": round(confidence, 2),
            "local_track_count": local_tc,
        }
    except Exception as exc:
        logger.error("Best release resolution error", error=str(exc), exc_info=True)
        return {"success": False, "error": str(exc)}
