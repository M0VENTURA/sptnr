"""MusicBrainz enrichment and lookup service.

Owns MusicBrainz interpretation and matching rules. This module performs no
track mutation. Database access is limited to read-only library comparison
helpers.

Operational improvements:
- Re-entrant singleton initialization lock to prevent startup deadlock.
- Structured start, completion, skip, failure, and slow-operation logs.
- Provider calls wrapped with heartbeat diagnostics.
- Atomic, bounded, timestamped MBID cache writes.
- Plain Cover Art Archive URLs instead of HTML-corrupted strings.
- Shared HTTP client used consistently by all singleton services.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

import structlog

try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _rapidfuzz_fuzz = None
    _HAVE_RAPIDFUZZ = False

from api_clients.musicbrainz_http import (
    MUSICBRAINZ_UUID_RE,
    MusicBrainzHttpClient,
    escape_lucene_special_chars,
)
from helpers.normalization_service import (
    edition_annotations_compatible,
    normalize_string,
    normalize_title_for_lookup,
    normalize_title_for_lucene_query,
    normalize_title_for_mbid_match,
    strip_featured_artist,
    strip_search_keywords,
    strip_single_release_suffix,
)

logger = structlog.get_logger(__name__)
T = TypeVar("T")

CACHE_FILE = os.getenv(
    "MUSICBRAINZ_CACHE_FILE",
    "/tmp/mbid_cache.json" if os.path.exists("/tmp") else "mbid_cache.json",
)
_HEARTBEAT_SECONDS = max(5.0, float(os.getenv("MUSICBRAINZ_HEARTBEAT_SECONDS", "30")))
_CACHE_IO_LOCK = threading.Lock()
# Must be re-entrant: shared-service initialization obtains this lock and then
# asks for the shared HTTP client, which obtains the same lock.
_INIT_LOCK = threading.RLock()
_MB_BATCH_CHUNK = 20
_MB_BATCH_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_SIMILARITY_FLOOR = 0.6
_MBID_CACHE_TTL_SECONDS = 30 * 24 * 3600
_MBID_CACHE_MAX_SIZE = 5000

_COMPARE_LIBRARY_TRACKS_SQL = """
    SELECT id, title, track_number, disc_number, artist, year,
           mbid, file_path, duration, mb_ignored_fields
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
"""


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@contextmanager
def _logged_section(section: str, **context: Any) -> Iterator[None]:
    started = time.monotonic()
    logger.info("[MB] section started", section=section, **context)
    try:
        yield
    except Exception as exc:
        logger.exception(
            "[MB] section failed", section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            error=_error(exc), **context,
        )
        raise
    else:
        logger.info(
            "[MB] section completed", section=section,
            elapsed_s=round(time.monotonic() - started, 3), **context,
        )


def _call_with_heartbeat(
    section: str,
    func: Callable[..., T],
    *args: Any,
    log_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    """Execute synchronously and report periodically if the call is slow.

    This does not cancel a blocked request. Network timeouts belong in
    MusicBrainzHttpClient. It identifies the exact call that has not returned.
    """
    context = dict(log_context or {})
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(_HEARTBEAT_SECONDS):
            logger.warning(
                "[MB] call still running", section=section,
                elapsed_s=round(time.monotonic() - started, 1), **context,
            )

    logger.info("[MB] call started", section=section, **context)
    monitor = threading.Thread(
        target=heartbeat, name=f"mb-heartbeat-{section}", daemon=True,
    )
    monitor.start()
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        logger.exception(
            "[MB] call failed", section=section,
            elapsed_s=round(time.monotonic() - started, 3),
            error=_error(exc), **context,
        )
        raise
    else:
        logger.info(
            "[MB] call completed", section=section,
            elapsed_s=round(time.monotonic() - started, 3), **context,
        )
        return result
    finally:
        stopped.set()
        monitor.join(timeout=0.2)


def _similarity(a: str, b: str) -> float:
    from services.popularity.popularity_math import fuzzy_match_score
    return fuzzy_match_score(a, b)


def _mbid_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _HAVE_RAPIDFUZZ and _rapidfuzz_fuzz is not None:
        return _rapidfuzz_fuzz.token_sort_ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def build_artist_credit_string(artist_credit: list[Any]) -> str:
    parts: list[str] = []
    for credit in artist_credit or []:
        if isinstance(credit, dict):
            parts.extend((str(credit.get("name") or ""), str(credit.get("joinphrase") or "")))
        else:
            parts.append(str(credit))
    return "".join(parts).strip()


def primary_album_artist(artist_credit: list[Any] | str) -> str:
    if isinstance(artist_credit, list) and artist_credit:
        first = artist_credit[0]
        return str(first.get("name") or "").strip() if isinstance(first, dict) else str(first or "").strip()
    return artist_credit.strip() if isinstance(artist_credit, str) else ""


def calculate_match_score(
    mb_title: str,
    mb_artist_credit: list[Any] | str,
    local_album: str,
    local_artist: str,
) -> float:
    title_score = _similarity(normalize_string(local_album), normalize_string(mb_title))
    artist_name = primary_album_artist(mb_artist_credit)
    artist_score = _similarity(normalize_string(local_artist), normalize_string(artist_name))
    return (title_score * 0.6) + (artist_score * 0.4)


def _parse_secondary_types(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


def _compose_album_type(primary_type: str, secondary_types: list[str]) -> str:
    primary = str(primary_type or "album").strip().casefold() or "album"
    meaningful = {
        "compilation", "live", "remix", "soundtrack", "spokenword",
        "demo", "dj-mix", "mixtape", "interview", "audiobook", "ep",
    }
    secondary = next(
        (str(value).casefold() for value in secondary_types or [] if str(value).casefold() in meaningful),
        "",
    )
    return f"{primary}+{secondary}" if secondary else primary


def _artist_lookup_candidates(artist: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in (artist or "", strip_featured_artist(artist or "")):
        key = str(candidate or "").casefold().strip()
        if key and key not in seen:
            result.append(str(candidate).strip())
            seen.add(key)
    return result


def _normalise_artist_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _mb_artist_credit_name(artist_credit: list[Any] | str) -> str:
    return primary_album_artist(artist_credit)


def _cover_art_url(release_group_id: str = "", release_id: str = "") -> str:
    if release_group_id:
        return f"https://coverartarchive.org/release-group/{release_group_id}/front-250"
    if release_id:
        return f"https://coverartarchive.org/release/{release_id}/front-250"
    return ""


def _first_isrc(recording: dict[str, Any]) -> str | None:
    from helpers.normalization_service import normalize_isrc
    for raw in recording.get("isrcs") or recording.get("isrc-list") or []:
        value = normalize_isrc(raw)
        if value:
            return value
    return normalize_isrc(recording.get("isrc")) or None


def _recording_matches_album(recording: dict[str, Any], album: str) -> bool:
    album = str(album or "").strip().casefold()
    if not album or not recording:
        return False
    for release in recording.get("releases") or []:
        release_group = release.get("release-group") or {}
        candidates = (release.get("title") or "", release_group.get("title") or "")
        if any(candidate and _similarity(str(candidate), album) >= 0.6 for candidate in candidates):
            return True
    return False


_SHARED_MB_CLIENT: MusicBrainzHttpClient | None = None
_service: "MusicBrainzService | None" = None
_shared_mb_service: "MusicBrainzService | None" = None


def get_shared_mb_client() -> MusicBrainzHttpClient:
    global _SHARED_MB_CLIENT
    if _SHARED_MB_CLIENT is not None:
        logger.debug("[MB] shared HTTP client cache hit")
        return _SHARED_MB_CLIENT
    started = time.monotonic()
    logger.info("[MB] shared HTTP client initialization requested")
    with _INIT_LOCK:
        logger.info(
            "[MB] shared HTTP client initialization lock acquired",
            elapsed_s=round(time.monotonic() - started, 3),
        )
        if _SHARED_MB_CLIENT is None:
            with _logged_section("singleton.http_client.create"):
                _SHARED_MB_CLIENT = MusicBrainzHttpClient(enabled=True)
    logger.info("[MB] shared HTTP client ready", total_s=round(time.monotonic() - started, 3))
    return _SHARED_MB_CLIENT


class MusicBrainzService:
    def __init__(self, http_client: MusicBrainzHttpClient | None = None, enabled: bool = True):
        started = time.monotonic()
        logger.info("[MB] service construction started", enabled=enabled, supplied_client=http_client is not None)
        self.enabled = enabled
        self.http = http_client or MusicBrainzHttpClient(enabled=enabled)
        self._artist_singles_cache: dict[str, list[dict[str, Any]]] = {}
        self._mem_lock = threading.Lock()
        self._mbid_cache = self._load_cache()
        logger.info(
            "[MB] service construction completed", enabled=enabled,
            cache_entries=len(self._mbid_cache), elapsed_s=round(time.monotonic() - started, 3),
        )

    def _load_cache(self) -> dict[str, Any]:
        started = time.monotonic()
        logger.info("[MB] cache load started", cache_file=CACHE_FILE)
        with _CACHE_IO_LOCK:
            try:
                if not os.path.exists(CACHE_FILE):
                    logger.info("[MB] cache load skipped", reason="file not found", cache_file=CACHE_FILE)
                    return {}
                with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                output = {
                    key: value for key, value in raw.items()
                    if isinstance(raw, dict)
                    and isinstance(value, (list, tuple))
                    and len(value) >= 2
                    and str(value[0] or "").strip()
                }
                logger.info(
                    "[MB] cache load completed", cache_file=CACHE_FILE,
                    entries=len(output), elapsed_s=round(time.monotonic() - started, 3),
                )
                return output
            except Exception as exc:
                logger.exception("[MB] cache load failed", cache_file=CACHE_FILE, error=_error(exc))
                return {}

    def _save_cache(self) -> None:
        started = time.monotonic()
        with self._mem_lock:
            data = dict(self._mbid_cache)
        with _CACHE_IO_LOCK:
            try:
                tmp_path = f"{CACHE_FILE}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, CACHE_FILE)
                logger.info(
                    "[MB] cache save completed", cache_file=CACHE_FILE,
                    entries=len(data), elapsed_s=round(time.monotonic() - started, 3),
                )
            except Exception as exc:
                logger.exception("[MB] cache save failed", cache_file=CACHE_FILE, error=_error(exc))

    @staticmethod
    def _cache_key(title: str, artist: str) -> str:
        return f"{artist.casefold().strip()}::{title.casefold().strip()}"

    def get_suggested_mbid(self, title: str, artist: str, limit: int = 5) -> tuple[str, float]:
        context = {"artist": artist, "track": title, "limit": limit}
        if not self.enabled or not title or not artist:
            logger.info("[MB] recording suggestion skipped", reason="disabled or incomplete input", **context)
            return "", 0.0
        cache_key = self._cache_key(title, artist)
        now = time.time()
        with self._mem_lock:
            cached = self._mbid_cache.get(cache_key)
            if isinstance(cached, (list, tuple)) and len(cached) >= 2:
                mbid, score = str(cached[0] or ""), float(cached[1] or 0)
                cached_at = float(cached[2]) if len(cached) >= 3 else None
                if mbid and (cached_at is None or now - cached_at < _MBID_CACHE_TTL_SECONDS):
                    logger.info("[MB] recording suggestion cache hit", mbid=mbid, score=score, **context)
                    return mbid, round(score, 3)

        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = f'recording:"{escape_lucene_special_chars(query_title)}" AND artist:"{escape_lucene_special_chars(artist)}"'
        try:
            recordings = _call_with_heartbeat(
                "recording.search", self.http.search_recordings, query,
                limit=limit, log_context=context,
            ) or []
            best_mbid, best_score = "", 0.0
            normalized_title = normalize_title_for_mbid_match(title)
            for recording in recordings:
                candidate_title = str(recording.get("title") or "")
                if not edition_annotations_compatible(title, candidate_title):
                    continue
                score = _mbid_similarity(normalized_title, normalize_title_for_mbid_match(candidate_title))
                if score > best_score:
                    best_mbid, best_score = str(recording.get("id") or ""), score
            if best_mbid and best_score >= _MBID_CACHE_SIMILARITY_FLOOR:
                with self._mem_lock:
                    self._mbid_cache[cache_key] = (best_mbid, round(best_score, 3), now)
                    while len(self._mbid_cache) > _MBID_CACHE_MAX_SIZE:
                        try:
                            self._mbid_cache.pop(next(iter(self._mbid_cache)))
                        except (StopIteration, RuntimeError):
                            break
                self._save_cache()
            logger.info(
                "[MB] recording suggestion completed", mbid=best_mbid or None,
                score=round(best_score, 3), candidate_count=len(recordings), **context,
            )
            return best_mbid, round(best_score, 3)
        except Exception as exc:
            logger.exception("[MB] recording suggestion failed", error=_error(exc), **context)
            return "", 0.0

    def lookup_recording_metadata(self, title: str, artist: str) -> dict[str, Any]:
        context = {"title": title, "artist": artist}
        if not title or not artist:
            logger.info("[MB] recording metadata skipped", reason="incomplete input", **context)
            return {}
        try:
            mbid, confidence = self.get_suggested_mbid(title, artist)
            if not mbid:
                return {}
            recording = _call_with_heartbeat(
                "recording.get", self.http.get_recording, mbid,
                inc="artist-credits+releases+work-rels+genres",
                log_context={**context, "mbid": mbid},
            )
            return self._recording_to_metadata(recording, mbid, confidence) if recording else {}
        except Exception as exc:
            logger.exception("[MB] recording metadata lookup failed", error=_error(exc), **context)
            return {}

    def lookup_recordings_by_mbid_bulk(self, mbids: list[str]) -> dict[str, dict[str, Any]]:
        if not self.enabled or not mbids:
            return {}
        try:
            payload = _call_with_heartbeat(
                "recording.bulk_get", self.http.get_recordings_bulk, mbids,
                inc="artist-credits+releases+work-rels+genres",
                log_context={"mbid_count": len(mbids)},
            ) or {}
            results: dict[str, dict[str, Any]] = {}
            for recording in payload.get("recordings", []):
                mbid = str(recording.get("id") or "")
                if mbid:
                    results[mbid] = self._recording_to_metadata(recording, mbid, 1.0)
            logger.info("[MB] bulk recording lookup completed", requested=len(mbids), returned=len(results))
            return results
        except Exception as exc:
            logger.exception("[MB] bulk recording lookup failed", error=_error(exc), mbid_count=len(mbids))
            return {}

    def _recording_to_metadata(self, recording: dict[str, Any], mbid: str, confidence: float) -> dict[str, Any]:
        credits = recording.get("artist-credit") or []
        first = credits[0] if credits else {}
        artist = str(first.get("name") or "") if isinstance(first, dict) else str(first or "")
        artist_data = first.get("artist") or {} if isinstance(first, dict) else {}
        artist_mbid = str(artist_data.get("id") or "") if isinstance(artist_data, dict) else ""
        release = (recording.get("releases") or [None])[0]
        writers: list[str] = []
        work_mbid = ""
        for relation in recording.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            if str(relation.get("type") or "").casefold() not in {"performance", "recording of"}:
                continue
            work = relation.get("work") or {}
            work_mbid = str(work.get("id") or work_mbid)
            for work_relation in work.get("relations") or []:
                if str(work_relation.get("type") or "").casefold() in {"composer", "writer", "lyricist"}:
                    target = work_relation.get("artist") or {}
                    if target.get("name"):
                        writers.append(str(target["name"]))
        release_date = str((release or {}).get("date") or "")
        return {
            "title": recording.get("title"), "artist": artist,
            "artist_mbid": artist_mbid or None,
            "album": (release or {}).get("title"),
            "album_artist": primary_album_artist((release or {}).get("artist-credit") or []),
            "isrc": _first_isrc(recording),
            "year": int(release_date[:4]) if release_date[:4].isdigit() else None,
            "recording_mbid": mbid, "confidence": confidence,
            "writer": ", ".join(dict.fromkeys(writers)), "work_mbid": work_mbid,
            "genres": [str(g.get("name") or "").strip() for g in recording.get("genres") or [] if g.get("name")],
        }

    def lookup_album_metadata(
        self, entries: list[tuple[str, str]], candidates_per_entry: int = 5, album: str = "",
    ) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}
        unique = sorted({(str(t).strip(), str(a).strip()) for t, a in entries or [] if t and a})
        if not unique:
            return {}
        results: dict[str, dict[str, Any]] = {}
        logger.info("[MB] album recording batch started", entries=len(unique), album=album)
        for chunk_start in range(0, len(unique), _MB_BATCH_CHUNK):
            chunk = unique[chunk_start:chunk_start + _MB_BATCH_CHUNK]
            groups = [
                f'(recording:"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}" '
                f'AND artist:"{escape_lucene_special_chars(artist)}")'
                for title, artist in chunk
            ]
            try:
                recordings = _call_with_heartbeat(
                    "recording.batch_search", self.http.search_recordings, " OR ".join(groups),
                    limit=min(100, len(chunk) * candidates_per_entry),
                    inc="releases+work-rels+genres",
                    log_context={"chunk_start": chunk_start, "chunk_size": len(chunk), "album": album},
                ) or []
            except Exception:
                continue
            batch: list[tuple[str, str, float]] = []
            for title, artist in chunk:
                normalized = normalize_title_for_mbid_match(title)
                best: dict[str, Any] | None = None
                best_score, best_anchor = 0.0, False
                for recording in recordings:
                    candidate_title = str(recording.get("title") or "")
                    if not edition_annotations_compatible(title, candidate_title):
                        continue
                    score = _mbid_similarity(normalized, normalize_title_for_mbid_match(candidate_title))
                    anchor = _recording_matches_album(recording, album)
                    if score > best_score or (score == best_score and anchor and not best_anchor):
                        best, best_score, best_anchor = recording, score, anchor
                mbid = str((best or {}).get("id") or "")
                if mbid and best_score >= _MB_BATCH_SIMILARITY_FLOOR:
                    key = self._cache_key(title, artist)
                    confidence = round(best_score, 3)
                    batch.append((key, mbid, confidence))
                    with self._mem_lock:
                        self._mbid_cache[key] = (mbid, confidence, time.time())
            if batch:
                metadata = self.lookup_recordings_by_mbid_bulk([item[1] for item in batch])
                for key, mbid, confidence in batch:
                    if mbid in metadata:
                        results[key] = {**metadata[mbid], "confidence": confidence}
                self._save_cache()
        logger.info("[MB] album recording batch completed", entries=len(unique), matched=len(results), album=album)
        return results

    def is_single(self, title: str, artist: str, album_track_count: int | None = None) -> bool:
        del album_track_count
        if not self.enabled or not title or not artist:
            return False
        try:
            for candidate in _artist_lookup_candidates(artist):
                if self._recording_search_has_single_release(title, candidate):
                    return True
                mbid, _ = self.get_suggested_mbid(title, candidate)
                if mbid and self._recording_has_single_release(mbid, title):
                    return True
                if self._release_group_has_single_release(title, candidate):
                    return True
            return False
        except Exception as exc:
            logger.exception("[MB] single detection failed", artist=artist, track=title, error=_error(exc))
            return False

    def _release_group_has_single_release(self, title: str, artist: str) -> bool:
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = f'releasegroup:"{escape_lucene_special_chars(query_title)}" AND artist:"{escape_lucene_special_chars(artist)}"'
        groups = _call_with_heartbeat(
            "single.release_group_search", self.http.search_release_groups, query,
            limit=10, log_context={"artist": artist, "title": title},
        ) or []
        if not groups:
            groups = _call_with_heartbeat(
                "single.release_group_artist_fallback", self.http.search_release_groups,
                f'artist:"{escape_lucene_special_chars(artist)}"', limit=50,
                log_context={"artist": artist, "title": title},
            ) or []
        normalized = normalize_title_for_lookup(title)
        return any(
            str(group.get("primary-type") or group.get("primary_type") or group.get("type") or "").casefold() in {"single", "ep"}
            and edition_annotations_compatible(title, group.get("title") or "")
            and _similarity(normalized, normalize_title_for_lookup(group.get("title") or "")) >= 0.7
            for group in groups
        )

    def _recording_search_has_single_release(self, title: str, artist: str) -> bool:
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = f'recording:"{escape_lucene_special_chars(query_title)}" AND artist:"{escape_lucene_special_chars(artist)}"'
        recordings = _call_with_heartbeat(
            "single.recording_search", self.http.search_recordings, query,
            limit=10, log_context={"artist": artist, "title": title},
        ) or []
        for recording in recordings:
            for release in recording.get("releases") or []:
                group = release.get("release-group") or {}
                primary = str(group.get("primary-type") or group.get("primary_type") or group.get("type") or "").casefold()
                if primary in {"single", "ep"} and self._rg_title_matches(title, group.get("title") or ""):
                    return True
        return False

    @staticmethod
    def _rg_title_matches(title: str, release_group_title: str) -> bool:
        if not title or not release_group_title or not edition_annotations_compatible(title, release_group_title):
            return False
        left = normalize_title_for_lookup(strip_single_release_suffix(title) or title)
        right = normalize_title_for_lookup(strip_single_release_suffix(release_group_title) or release_group_title)
        return left == right or _similarity(left, right) >= 0.85

    def _recording_has_single_release(self, mbid: str, title: str = "") -> bool:
        recording = _call_with_heartbeat(
            "single.recording_get", self.http.get_recording, mbid,
            inc="releases+release-groups", log_context={"mbid": mbid, "title": title},
        )
        for release in (recording or {}).get("releases") or []:
            group = release.get("release-group") or {}
            primary = str(group.get("primary-type") or group.get("primary_type") or "").casefold()
            release_type = str(group.get("type") or "").casefold()
            if (primary in {"single", "ep"} or release_type in {"single", "ep"}) and (
                not title or self._rg_title_matches(title, group.get("title") or "")
            ):
                return True
        return False

    def get_artist_country(self, artist: str) -> str:
        if not self.enabled or not artist:
            return ""
        try:
            result = _call_with_heartbeat(
                "artist.country_search", self.http.search_artists,
                f'artist:"{escape_lucene_special_chars(artist)}"', limit=1, inc="area",
                log_context={"artist": artist},
            ) or []
            data = result[0] if result else {}
            return str((data.get("area") or {}).get("name") or (data.get("begin-area") or {}).get("name") or "")
        except Exception as exc:
            logger.exception("[MB] artist country lookup failed", artist=artist, error=_error(exc))
            return ""

    def get_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled:
            return []
        try:
            mbid, _ = self.get_suggested_mbid(title, artist)
            if not mbid:
                return []
            recording = _call_with_heartbeat(
                "recording.genres_get", self.http.get_recording, mbid,
                inc="genres", log_context={"artist": artist, "title": title, "mbid": mbid},
            )
            return [str(item["name"]) for item in (recording or {}).get("genres") or [] if item.get("name")]
        except Exception as exc:
            logger.exception("[MB] genre lookup failed", artist=artist, title=title, error=_error(exc))
            return []

    def search_releasegroup_matches(
        self, artist_name: str, album_name: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        context = {"artist": artist_name, "album": album_name, "limit": limit}
        logger.info("[MB] release-group matching started", enabled=self.enabled, **context)
        if not self.enabled or not artist_name or not album_name:
            logger.info("[MB] release-group matching skipped", reason="disabled or incomplete input", **context)
            return []
        clean_album = strip_search_keywords(album_name)
        escaped_artist = escape_lucene_special_chars(artist_name)
        exact_query = f'artist:"{escaped_artist}" AND releasegroup:"{escape_lucene_special_chars(clean_album)}"'
        try:
            groups = _call_with_heartbeat(
                "release_group.exact_search", self.http.search_release_groups,
                exact_query, limit=limit, log_context={**context, "query": exact_query},
            ) or []
        except Exception:
            groups = []
        if not groups and clean_album:
            terms = normalize_title_for_lucene_query(clean_album)
            if terms:
                fallback_query = f'artist:"{escaped_artist}" AND releasegroup:{terms}'
                try:
                    groups = _call_with_heartbeat(
                        "release_group.fallback_search", self.http.search_release_groups,
                        fallback_query, limit=limit,
                        log_context={**context, "query": fallback_query},
                    ) or []
                except Exception:
                    groups = []
        matches: list[dict[str, Any]] = []
        with _logged_section("release_group.scoring", candidate_count=len(groups), **context):
            for index, group in enumerate(groups):
                try:
                    score = calculate_match_score(
                        str(group.get("title") or ""), group.get("artist-credit") or [],
                        album_name, artist_name,
                    )
                    match = {
                        "id": group.get("id"), "title": group.get("title"),
                        "primary_type": group.get("primary-type"),
                        "match_score": round(score, 3),
                        "secondary_types": _parse_secondary_types(group.get("secondary-types")),
                    }
                    matches.append(match)
                    logger.debug("[MB] release-group candidate scored", candidate_index=index, **match, **context)
                except Exception as exc:
                    logger.exception("[MB] release-group candidate scoring failed", candidate_index=index, error=_error(exc), **context)
        matches.sort(key=lambda item: item.get("match_score", 0.0), reverse=True)
        best = matches[0] if matches else {}
        logger.info(
            "[MB] release-group matching completed", raw_candidate_count=len(groups),
            match_count=len(matches), best_match_id=best.get("id"),
            best_match_title=best.get("title"), best_match_score=best.get("match_score"),
            total_s=round(time.monotonic() - started, 3), **context,
        )
        return matches

    @staticmethod
    def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}
        def pick(*values: Any) -> Any:
            return next((value for value in values if value), None)
        return {
            key: pick(overrides.get(key), mb.get(key), base.get(key))
            for key in ("title", "artist", "album", "album_artist", "year")
        }

    def get_artist_relationships(self, artist_mbid: str, relation_type: str = "artist") -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []
        inc = {"artist": "artist-rels", "recording": "recording-rels", "work": "work-rels"}.get(relation_type, "artist-rels")
        try:
            data = _call_with_heartbeat(
                "artist.relationships_get", self.http.get_artist, artist_mbid,
                inc=inc, log_context={"artist_mbid": artist_mbid, "relation_type": relation_type},
            ) or {}
            return data.get("relations", []) or []
        except Exception as exc:
            logger.exception("[MB] artist relationships failed", artist_mbid=artist_mbid, error=_error(exc))
            return []

    def get_recording_relationships(self, recording_mbid: str) -> list[dict[str, Any]]:
        if not self.enabled or not recording_mbid:
            return []
        try:
            data = _call_with_heartbeat(
                "recording.relationships_get", self.http.get_recording, recording_mbid,
                inc="artist-rels+work-rels+work-level-rels+recording-level-rels",
                log_context={"recording_mbid": recording_mbid},
            ) or {}
            return data.get("relations", []) or []
        except Exception as exc:
            logger.exception("[MB] recording relationships failed", recording_mbid=recording_mbid, error=_error(exc))
            return []

    def get_composers_for_recording(self, recording_mbid: str) -> list[str]:
        composers: list[str] = []
        for relation in self.get_recording_relationships(recording_mbid):
            if str(relation.get("type") or "").casefold() in {"composer", "writer", "lyricist"}:
                target = relation.get("artist") or {}
                if target.get("name"):
                    composers.append(str(target["name"]))
            for work_relation in (relation.get("work") or {}).get("relations") or []:
                if str(work_relation.get("type") or "").casefold() in {"composer", "writer", "lyricist"}:
                    target = work_relation.get("artist") or {}
                    if target.get("name"):
                        composers.append(str(target["name"]))
        return list(dict.fromkeys(composers))

    def get_recording_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled or not title or not artist:
            return []
        query = f'recording:"{escape_lucene_special_chars(title)}" AND artist:"{escape_lucene_special_chars(artist)}"'
        try:
            recordings = _call_with_heartbeat(
                "recording.genre_search", self.http.search_recordings_with_genres,
                query, limit=3, log_context={"artist": artist, "title": title},
            ) or []
            genres: list[str] = []
            for recording in recordings:
                for genre in recording.get("genres") or []:
                    name = genre.get("name") if isinstance(genre, dict) else str(genre)
                    if name and name not in genres:
                        genres.append(str(name))
            return genres
        except Exception as exc:
            logger.exception("[MB] recording genre search failed", artist=artist, title=title, error=_error(exc))
            return []


def _get_service() -> MusicBrainzService:
    global _service
    if _service is not None:
        return _service
    logger.info("[MB] default service initialization requested")
    with _INIT_LOCK:
        if _service is None:
            with _logged_section("singleton.default_service.create"):
                _service = MusicBrainzService(http_client=get_shared_mb_client(), enabled=True)
    return _service


def get_shared_mb_service() -> MusicBrainzService:
    global _shared_mb_service
    if _shared_mb_service is not None:
        logger.debug("[MB] shared service cache hit")
        return _shared_mb_service
    started = time.monotonic()
    logger.info("[MB] shared service initialization requested")
    with _INIT_LOCK:
        logger.info(
            "[MB] shared service initialization lock acquired",
            elapsed_s=round(time.monotonic() - started, 3),
        )
        if _shared_mb_service is None:
            client = get_shared_mb_client()
            with _logged_section("singleton.shared_service.create"):
                _shared_mb_service = MusicBrainzService(http_client=client, enabled=True)
    logger.info("[MB] shared service ready", total_s=round(time.monotonic() - started, 3))
    return _shared_mb_service


def lookup_recording_metadata(title: str, artist: str) -> dict[str, Any]:
    return _get_service().lookup_recording_metadata(title, artist)


def merge_metadata(base: dict[str, Any], mb: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return _get_service().merge_metadata(base, mb, overrides)


def fetch_musicbrainz_release_metadata(release_id: str) -> dict[str, Any] | None:
    context = {"release_id": release_id}
    try:
        data = _call_with_heartbeat(
            "release.metadata_get", get_shared_mb_client().get_release, release_id,
            inc="recordings+artist-credits+release-groups+work-rels+genres",
            log_context=context,
        )
        if not data:
            logger.info("[MB] release metadata not found", **context)
            return None
        release_group = data.get("release-group") or {}
        group_date = str(release_group.get("first-release-date") or "")
        release_date = str(data.get("date") or "")
        secondary_types = _parse_secondary_types(release_group.get("secondary-types"))
        info: dict[str, Any] = {
            "release_title": release_group.get("title") or data.get("title"),
            "release_year": (group_date or release_date)[:4],
            "artist": "", "disc_count": len(data.get("media") or []), "tracks": [],
            "release_mbid": data.get("id"), "release_group_mbid": release_group.get("id") or "",
            "compilation": int("compilation" in {value.casefold() for value in secondary_types}),
            "original_date": group_date or release_date,
            "original_year": (group_date or release_date)[:4],
            "album_type": _compose_album_type(release_group.get("primary-type") or "Album", secondary_types),
        }
        credits = data.get("artist-credit") or []
        if credits:
            info["artist"] = primary_album_artist(credits)
            info["artist_credit"] = build_artist_credit_string(credits)
            artist_data = (credits[0] or {}).get("artist") or {} if isinstance(credits[0], dict) else {}
            info["album_artist_mbid"] = str(artist_data.get("id") or "")
        absolute_number = 1
        for disc_index, medium in enumerate(data.get("media") or [], 1):
            for track in medium.get("tracks") or []:
                recording = track.get("recording") or {}
                track_info: dict[str, Any] = {
                    "disc_number": disc_index, "track_number": track.get("position"),
                    "absolute_track_number": absolute_number,
                    "title": track.get("title") or recording.get("title"),
                    "recording_mbid": recording.get("id"), "duration": track.get("length"),
                    "artist": build_artist_credit_string(recording.get("artist-credit") or []) or info.get("artist_credit") or info.get("artist"),
                }
                absolute_number += 1
                writers: list[str] = []
                composers: list[str] = []
                lyricists: list[str] = []
                for relation in recording.get("relations") or []:
                    if str(relation.get("type") or "").casefold() not in {"performance", "recording of"}:
                        continue
                    work = relation.get("work") or {}
                    track_info["work_mbid"] = work.get("id") or ""
                    track_info["work_title"] = work.get("title") or ""
                    track_info["iswc"] = str(work.get("iswc") or "")
                    work_artist = primary_album_artist(work.get("artist-credit") or [])
                    if work_artist:
                        track_info["work_artist"] = work_artist
                        if _normalise_artist_key(work_artist) != _normalise_artist_key(track_info["artist"]):
                            track_info.update(is_cover=True, original_cover_artist=work_artist, original_title=work.get("title") or "")
                    for work_relation in work.get("relations") or []:
                        relation_type = str(work_relation.get("type") or "").casefold()
                        name = str((work_relation.get("artist") or {}).get("name") or "")
                        if not name:
                            continue
                        if relation_type == "composer":
                            composers.append(name)
                        if relation_type in {"writer", "lyricist"}:
                            lyricists.append(name)
                        if relation_type in {"composer", "writer", "lyricist"}:
                            writers.append(name)
                if composers:
                    track_info["composer"] = ", ".join(dict.fromkeys(composers))
                if lyricists:
                    track_info["lyricist"] = ", ".join(dict.fromkeys(lyricists))
                if writers:
                    track_info["writer"] = ", ".join(dict.fromkeys(writers))
                genres = [str(g.get("name") or "").strip() for g in recording.get("genres") or [] if g.get("name")]
                if genres:
                    track_info["musicbrainz_genres"] = ", ".join(dict.fromkeys(genres))
                info["tracks"].append(track_info)
        try:
            from api_clients.coverartarchive import get_release_front_image_bytes
            cover = _call_with_heartbeat(
                "release.cover_art_get", get_release_front_image_bytes,
                release_id, log_context=context,
            )
            if cover:
                info["cover_art"] = cover
        except Exception as exc:
            logger.warning("[MB] release cover-art lookup failed", error=_error(exc), **context)
        logger.info("[MB] release metadata completed", track_count=len(info["tracks"]), **context)
        return info
    except Exception as exc:
        logger.exception("[MB] release metadata failed", error=_error(exc), **context)
        return None


def fetch_release_metadata(release_id: str) -> dict[str, Any] | None:
    return fetch_musicbrainz_release_metadata(release_id)


def resolve_release_id(release_id: str) -> str:
    if not release_id:
        return release_id
    context = {"release_or_group_id": release_id}
    client = get_shared_mb_client()
    try:
        data = _call_with_heartbeat("release.resolve.direct", client.get_release, release_id, inc="", log_context=context)
        if data and data.get("id"):
            logger.info("[MB] release ID already identifies a release", **context)
            return release_id
    except Exception as exc:
        logger.info("[MB] direct release resolution did not match", error=_error(exc), **context)
    try:
        releases = _call_with_heartbeat(
            "release.resolve.group_browse", client.browse_releases_for_group,
            release_id, inc="media", limit=50, log_context=context,
        ) or []
        def track_count(release: dict[str, Any]) -> int:
            return sum(int(medium.get("track-count") or 0) for medium in release.get("media") or [])
        official = [release for release in releases if str(release.get("status") or "").casefold() == "official"]
        candidates = [release for release in (official or releases) if track_count(release) > 0] or official or releases
        if candidates:
            best = max(candidates, key=track_count)
            resolved = str(best.get("id") or release_id)
            logger.info("[MB] release group resolved", resolved_id=resolved, tracks=track_count(best), **context)
            return resolved
    except Exception as exc:
        logger.exception("[MB] release-group resolution failed", error=_error(exc), **context)
    return release_id


def _lookup_existing_mbid(existing_mbid: str, artist: str, album: str) -> dict[str, Any] | None:
    if not existing_mbid:
        return None
    client = get_shared_mb_client()
    context = {"existing_mbid": existing_mbid, "artist": artist, "album": album}
    try:
        data = _call_with_heartbeat(
            "album.existing_release_get", client.get_release, existing_mbid,
            inc="artist-credits+release-groups", log_context=context,
        )
        if data:
            group = data.get("release-group") or {}
            return {
                "mbid": existing_mbid, "title": group.get("title") or data.get("title", album),
                "artist": primary_album_artist(data.get("artist-credit") or []) or artist,
                "primary_type": group.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(group.get("secondary-types")),
                "first_release_date": group.get("first-release-date") or data.get("date") or "",
                "cover_art_url": _cover_art_url(str(group.get("id") or ""), existing_mbid),
                "confidence": 1.0, "source": "musicbrainz", "is_stored_mbid": True, "mbid_type": "release",
            }
    except Exception as exc:
        logger.info("[MB] stored MBID was not a release", error=_error(exc), **context)
    try:
        data = _call_with_heartbeat(
            "album.existing_release_group_get", client.get_release_group,
            existing_mbid, inc="artist-credits", log_context=context,
        )
        if data:
            return {
                "mbid": existing_mbid, "title": data.get("title", album),
                "artist": _mb_artist_credit_name(data.get("artist-credit") or []) or artist,
                "primary_type": data.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(data.get("secondary-types")),
                "first_release_date": data.get("first-release-date", ""),
                "cover_art_url": _cover_art_url(existing_mbid),
                "confidence": 1.0, "source": "musicbrainz", "is_stored_mbid": True, "mbid_type": "release-group",
            }
    except Exception as exc:
        logger.exception("[MB] stored MBID lookup failed", error=_error(exc), **context)
    return None


def lookup_musicbrainz_album(artist: str, album: str, existing_mbid: str = "") -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if existing_mbid:
        stored = _lookup_existing_mbid(existing_mbid, artist, album)
        if stored:
            results.append(stored)
    query = f'release:"{escape_lucene_special_chars(album)}" AND artist:"{escape_lucene_special_chars(artist)}"'
    try:
        groups = _call_with_heartbeat(
            "album.release_group_search", get_shared_mb_client().search_release_groups,
            query, limit=10, log_context={"artist": artist, "album": album},
        ) or []
    except Exception:
        groups = []
    seen = {item["mbid"] for item in results}
    for group in groups:
        group_id = str(group.get("id") or "")
        if not group_id or group_id in seen:
            continue
        results.append({
            "mbid": group_id, "title": group.get("title", ""),
            "artist": _mb_artist_credit_name(group.get("artist-credit") or []),
            "primary_type": group.get("primary-type", "Album"),
            "secondary_types": _parse_secondary_types(group.get("secondary-types")),
            "first_release_date": group.get("first-release-date", ""),
            "cover_art_url": _cover_art_url(group_id),
            "confidence": round(calculate_match_score(group.get("title") or "", group.get("artist-credit") or [], album, artist), 3),
            "source": "musicbrainz", "is_stored_mbid": False, "mbid_type": "release-group",
        })
        seen.add(group_id)
    stored = [item for item in results if item.get("is_stored_mbid")]
    others = sorted([item for item in results if not item.get("is_stored_mbid")], key=lambda item: item.get("confidence", 0), reverse=True)
    return {"results": (stored + others)[:11]}


def get_release_group_releases(release_group_mbid: str, include_track_counts: bool = False) -> dict[str, Any]:
    try:
        data = _call_with_heartbeat(
            "release_group.get_releases", get_shared_mb_client().get_release_group,
            release_group_mbid, inc="releases", log_context={"release_group_mbid": release_group_mbid},
        )
        if not data:
            return {"success": False, "error": "No release-group data returned"}
        releases: list[dict[str, Any]] = []
        for release in data.get("releases") or []:
            media = release.get("media") or []
            release_id = str(release.get("id") or "")
            releases.append({
                "id": release_id, "title": release.get("title", ""), "date": release.get("date", ""),
                "country": release.get("country", ""), "status": release.get("status", ""),
                "disambiguation": release.get("disambiguation", ""),
                "track_count": sum(int(medium.get("track-count") or 0) for medium in media),
                "disc_count": len(media),
                "formats": sorted({str(medium.get("format") or "").strip() for medium in media if medium.get("format")}),
                "cover_art_url": _cover_art_url(release_id=release_id),
            })
        if include_track_counts and releases:
            _enrich_releases_with_track_counts(releases, release_group_mbid)
        return {"success": True, "releases": releases}
    except Exception as exc:
        logger.exception("[MB] release-group releases lookup failed", release_group_mbid=release_group_mbid, error=_error(exc))
        return {"success": False, "error": str(exc)}


def _enrich_releases_with_track_counts(releases: list[dict[str, Any]], release_group_mbid: str | None = None) -> None:
    if not releases or not release_group_mbid:
        return
    try:
        browsed = _call_with_heartbeat(
            "release_group.track_counts", get_shared_mb_client().browse_releases_for_group,
            release_group_mbid, inc="media", limit=100,
            log_context={"release_group_mbid": release_group_mbid},
        ) or []
        counts = {
            str(release.get("id")): sum(int(medium.get("track-count") or 0) for medium in release.get("media") or [])
            for release in browsed if release.get("id")
        }
        for release in releases:
            if str(release.get("id")) in counts and counts[str(release.get("id"))] > 0:
                release["track_count"] = counts[str(release.get("id"))]
    except Exception as exc:
        logger.exception("[MB] release track-count enrichment failed", release_group_mbid=release_group_mbid, error=_error(exc))


def _get_local_track_count(artist: str, album: str) -> int:
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            row = session.execute(
                text("SELECT COUNT(*) FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) AND LOWER(COALESCE(album, '')) = LOWER(:album)"),
                {"artist": artist, "album": album},
            ).first()
            return int(row[0]) if row else 0
    except Exception as exc:
        logger.exception("[MB] local track count failed", artist=artist, album=album, error=_error(exc))
        return 0


def get_musicbrainz_best_release(artist: str, album: str, release_group_mbid: str) -> dict[str, Any]:
    try:
        raw = _call_with_heartbeat(
            "release_group.best_release_browse", get_shared_mb_client().browse_releases_for_group,
            release_group_mbid, inc="media+labels", limit=50,
            log_context={"artist": artist, "album": album, "release_group_mbid": release_group_mbid},
        ) or []
        releases: list[dict[str, Any]] = []
        for release in raw:
            media = release.get("media") or []
            release_id = str(release.get("id") or "")
            releases.append({
                "id": release_id, "title": release.get("title", ""), "date": release.get("date", ""),
                "country": release.get("country", ""), "status": release.get("status", ""),
                "disambiguation": release.get("disambiguation", ""),
                "track_count": sum(int(medium.get("track-count") or 0) for medium in media),
                "disc_count": len(media),
                "formats": sorted({str(medium.get("format") or "").strip() for medium in media if medium.get("format")}),
                "cover_art_url": _cover_art_url(release_id=release_id),
            })
        releases.sort(key=lambda item: (not bool(item.get("date")), item.get("date") or ""))
        if not releases:
            return {"success": True, "releases": [], "best_release": None, "confidence": 0, "local_track_count": None}
        local_count = _get_local_track_count(artist, album) or None
        def score(item: dict[str, Any]) -> float:
            value = -abs(local_count - int(item.get("track_count") or 0)) * 100.0 if local_count is not None else 0.0
            if str(item.get("status") or "").casefold() == "official":
                value += 50.0
            date = str(item.get("date") or "")
            if date[:4].isdigit():
                value += max(0.0, 2100.0 - int(date[:4])) * 0.01
            if album and item.get("title"):
                value += _similarity(album.casefold(), str(item["title"]).casefold()) * 30.0
            return value
        best = max(releases, key=score)
        confidence = 0.5 if local_count is None else max(0.0, 1.0 - abs(local_count - int(best.get("track_count") or 0)) * 0.2)
        return {
            "success": True, "releases": releases, "best_release": best,
            "confidence": round(confidence, 2), "local_track_count": local_count,
        }
    except Exception as exc:
        logger.exception("[MB] best release resolution failed", error=_error(exc), artist=artist, album=album, release_group_mbid=release_group_mbid)
        return {"success": False, "error": str(exc)}


def compare_musicbrainz_release(artist: str, album: str, release_group_mbid: str) -> dict[str, Any]:
    """Compare a chosen MusicBrainz release with local tracks.

    Matching order is disc/track number, exact normalized title, fuzzy title,
    then cross-disc fuzzy title. Differences respect mb_ignored_fields.
    """
    started = time.monotonic()
    context = {"artist": artist, "album": album, "release_group_mbid": release_group_mbid}
    logger.info("[MB] release comparison started", **context)
    try:
        client = get_shared_mb_client()
        try:
            direct = _call_with_heartbeat("compare.direct_release_get", client.get_release, release_group_mbid, inc="", log_context=context)
        except Exception:
            direct = None
        if direct and direct.get("id"):
            release_id = release_group_mbid
        else:
            best_result = get_musicbrainz_best_release(artist, album, release_group_mbid)
            release_id = str(((best_result or {}).get("best_release") or {}).get("id") or "")
            if not release_id:
                release_id = resolve_release_id(release_group_mbid)
        mb_release = fetch_musicbrainz_release_metadata(release_id)
        if not mb_release:
            return {"success": False, "error": "Could not fetch MusicBrainz release data"}

        from db.engine import db_session
        from sqlalchemy import text
        with _logged_section("compare.library_tracks_read", **context):
            with db_session() as session:
                rows = session.execute(text(_COMPARE_LIBRARY_TRACKS_SQL), {"artist": artist, "album": album}).mappings().all()
                library = [dict(row) for row in rows]
        if not library:
            return {"success": False, "error": "No library tracks found for this album", "comparison": []}

        def number(value: Any, default: int) -> int:
            try:
                return int(str(value or "").split("/")[0].strip())
            except (TypeError, ValueError):
                return default
        def normalized(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "").casefold().strip())
        def core(value: Any) -> str:
            return re.sub(r"\s*[\(\[].+$", "", normalized(value)).strip()
        def seconds(value: Any) -> float | None:
            try:
                result = float(value)
                result = result / 1000.0 if result > 10000 else result
                return result if result > 0 else None
            except (TypeError, ValueError):
                return None
        def duration_display(value: float | None) -> str | None:
            if value is None:
                return None
            rounded = int(round(value))
            return f"{rounded // 60}:{rounded % 60:02d}"
        def ignored_fields(track: dict[str, Any]) -> set[str]:
            try:
                parsed = json.loads(track.get("mb_ignored_fields") or "[]")
                return {str(item) for item in parsed} if isinstance(parsed, list) else set()
            except Exception:
                return set()

        library.sort(key=lambda track: (number(track.get("disc_number"), 1), number(track.get("track_number"), 999)))
        by_number = {(number(track.get("disc_number"), 1), number(track.get("track_number"), 999)): track for track in library}
        by_title = {(number(track.get("disc_number"), 1), normalized(track.get("title"))): track for track in library}
        matched_ids: set[Any] = set()
        comparison: list[dict[str, Any]] = []
        mb_year = str(mb_release.get("release_year") or "")

        for mb_track in mb_release.get("tracks") or []:
            disc = number(mb_track.get("disc_number"), 1)
            track_number = number(mb_track.get("track_number"), -1)
            mb_title = str(mb_track.get("title") or "")
            norm_title, core_title = normalized(mb_title), core(mb_title)
            candidate = by_number.get((disc, track_number)) if track_number >= 0 else None
            if candidate and difflib.SequenceMatcher(None, norm_title, normalized(candidate.get("title"))).ratio() < 0.30:
                candidate = None
            candidate = candidate or by_title.get((disc, norm_title))
            if candidate is None:
                candidates = [track for track in library if number(track.get("disc_number"), 1) == disc and track.get("id") not in matched_ids]
                scored = [(difflib.SequenceMatcher(None, norm_title, normalized(track.get("title"))).ratio(), track) for track in candidates]
                if scored:
                    best_score, best_candidate = max(scored, key=lambda item: item[0])
                    candidate = best_candidate if best_score >= 0.80 else None
            if candidate is None and core_title != norm_title:
                candidate = by_title.get((disc, core_title))

            mb_duration = seconds(mb_track.get("duration"))
            entry: dict[str, Any] = {
                "mb_track_number": None if track_number < 0 else track_number,
                "mb_disc_number": disc, "mb_title": mb_title,
                "mb_artist": mb_track.get("artist") or "",
                "mb_recording_id": str(mb_track.get("recording_mbid") or ""),
                "mb_year": mb_year, "mb_duration": duration_display(mb_duration),
                "mb_duration_sec": int(mb_duration) if mb_duration else None,
                "mb_writer": mb_track.get("writer") or "", "mb_work_mbid": mb_track.get("work_mbid") or "",
                "mb_work_title": mb_track.get("work_title") or "", "mb_work_artist": mb_track.get("work_artist") or "",
                "mb_is_cover": bool(mb_track.get("is_cover")),
                "mb_original_cover_artist": mb_track.get("original_cover_artist") or "",
                "mb_musicbrainz_genres": mb_track.get("musicbrainz_genres") or "",
                "mb_artist_credit": mb_track.get("artist") or "",
                "library_track_id": None, "library_title": None, "library_track_number": None,
                "library_disc_number": None, "library_artist": None, "library_year": None,
                "library_duration": None, "matched": False, "needs_update": False, "diff_fields": [],
            }
            if candidate is not None and candidate.get("id") not in matched_ids:
                matched_ids.add(candidate["id"])
                local_duration = seconds(candidate.get("duration"))
                entry.update({
                    "matched": True, "library_track_id": candidate.get("id"),
                    "library_title": candidate.get("title", ""), "library_track_number": candidate.get("track_number"),
                    "library_disc_number": number(candidate.get("disc_number"), 1),
                    "library_artist": candidate.get("artist", ""), "library_year": str(candidate.get("year") or ""),
                    "library_duration": duration_display(local_duration),
                })
                differences: list[str] = []
                local_title = str(candidate.get("title") or "")
                stripped_cover = re.sub(r"\s*\([^)]*\bcover\b[^)]*\)", "", local_title, flags=re.IGNORECASE).strip()
                if mb_title and mb_title.casefold() != local_title.casefold() and stripped_cover.casefold() != mb_title.casefold():
                    differences.append("title")
                if track_number >= 0 and str(track_number) != str(candidate.get("track_number") or ""):
                    differences.append("track_number")
                if mb_year and mb_year != str(candidate.get("year") or ""):
                    differences.append("year")
                if entry["mb_recording_id"] and not str(candidate.get("mbid") or "").strip():
                    differences.append("mbid")
                if mb_duration is not None and local_duration is not None and abs(mb_duration - local_duration) > 5.0:
                    differences.append("duration")
                differences = [item for item in differences if item not in ignored_fields(candidate)]
                entry["diff_fields"] = differences
                entry["needs_update"] = bool(differences)
            comparison.append(entry)

        extras = [
            {
                "library_track_id": track["id"], "library_title": track.get("title", ""),
                "library_track_number": track.get("track_number"),
                "library_disc_number": number(track.get("disc_number"), 1),
                "library_artist": track.get("artist", ""),
            }
            for track in library if track["id"] not in matched_ids
        ]
        result = {
            "success": True, "mb_title": str(mb_release.get("release_title") or ""),
            "mb_year": mb_year, "mb_artist": str(mb_release.get("artist") or ""),
            "mb_release_mbid": str(mb_release.get("release_mbid") or release_id),
            "mb_release_group_mbid": release_group_mbid, "release_group_mbid": release_group_mbid,
            "release_mbid": release_id, "mb_album_artist_mbid": str(mb_release.get("album_artist_mbid") or ""),
            "mb_albumtype": str(mb_release.get("album_type") or ""),
            "mb_disc_count": int(mb_release.get("disc_count") or 0),
            "mb_artist_credit": str(mb_release.get("artist_credit") or ""),
            "comparison": comparison, "extra_tracks": extras,
            "tracks_needing_update": sum(1 for item in comparison if item.get("needs_update")),
            "total_tracks": len(comparison),
        }
        logger.info(
            "[MB] release comparison completed", matched=sum(1 for item in comparison if item.get("matched")),
            extras=len(extras), updates=result["tracks_needing_update"],
            elapsed_s=round(time.monotonic() - started, 3), **context,
        )
        return result
    except Exception as exc:
        logger.exception("[MB] release comparison failed", error=_error(exc), **context)
        return {"success": False, "error": str(exc)}
