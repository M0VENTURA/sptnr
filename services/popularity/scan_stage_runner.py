"""Staged popularity scan runner."""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

import structlog

from helpers.config_helpers import get_config
from helpers.logging_config import log_unified
from helpers.normalization_service import strip_featured_artist
from services.popularity.progress_tracker import finish, start, update
from services.popularity.popularity_cache_policy import should_freeze_track
from services.popularity.scan_hooks import (
    apply_context_fields_to_track,
    get_stat_eligible_tracks,
    prepare_tracks_for_album,
)
from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_math import (
    ALBUM_RELATIVE_MIN_ALBUM_TRACKS,
    apply_album_relative_popularity,
    apply_track_artist_relative_popularity,
)
from services.popularity.popularity_sources import (
    get_lastfm_artist_max_listeners,
)
from services.popularity.stages.album_stage import enrich_album
from services.popularity.stages.finalise_stage import (
    _create_essential_m3u,
    _essential_playlists_enabled,
    _essential_strip_guest_credit,
    _fetch_essential_featured_rows,
    finalise_scan,
    post_album_star_ratings,
)
from services.popularity.stages.load_stage import load_candidates
from services.popularity.stages.track_stage import process_track
from db.repositories.tracks import DeferredPersistSink, upsert_tracks_bulk
from services.scanning.scan_state import (
    is_stop_requested,
    save_artist_scan_checkpoint,
    write_progress_with_current_artist,
    get_scan_progress_path,
)
from services.scanning.scan_history_service import record_scan, was_album_scanned
from services.catalog.album_classification_service import (
    detect_live_album_type,
    is_bonus_track_title,
    is_instrumental_track_title,
    should_exclude_track_from_stats,
)

logger = structlog.get_logger(__name__)


def _is_comp_artist(artist_name: str) -> bool:
    """Helper to detect compilation artists."""
    if not artist_name:
        return False
    return artist_name.strip().lower() in (
        "various artists", "various artists -", "various", 
        "compilation", "soundtrack"
    )


def _bounded_call(fn, seconds: float, label: str) -> None:
    """Run ``fn()`` with a hard time budget so a hung call cannot freeze a scan."""
    import threading

    if seconds is None or seconds <= 0:
        fn()
        return

    _done = threading.Event()
    _error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            _error["exc"] = exc
        finally:
            _done.set()

    _thread = threading.Thread(target=_runner, name=f"bounded-{label[:40]}", daemon=True)
    _thread.start()
    if not _done.wait(seconds):
        logger.warning(
            "Budget exceeded — abandoned, scan continues",
            task=label,
            budget_seconds=seconds,
        )
        return
    if _error.get("exc") is not None:
        logger.warning("Bounded call failed", task=label, error=str(_error["exc"]))


def _bounded_call_result(fn, seconds: float, label: str, default: Any = None) -> Any:
    """Run ``fn()`` with a hard time budget, returning its result or ``default``.

    Unlike ``_bounded_call`` (fire-and-forget), this captures the call's
    return value so callers can fall back gracefully when a hung operation
    (e.g. album enrichment making network calls) exceeds the budget — the
    scan continues with ``default`` instead of freezing on that album.
    """
    import threading

    if seconds is None or seconds <= 0:
        try:
            return fn()
        except BaseException:  # noqa: BLE001
            return default

    _done = threading.Event()
    _box: dict[str, Any] = {"result": default, "exc": None}

    def _runner() -> None:
        try:
            _box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            _box["exc"] = exc
        finally:
            _done.set()

    _thread = threading.Thread(target=_runner, name=f"bounded-{label[:40]}", daemon=True)
    _thread.start()
    if not _done.wait(seconds):
        logger.warning(
            "Budget exceeded — abandoned, using default",
            task=label,
            budget_seconds=seconds,
        )
        return default
    if _box["exc"] is not None:
        logger.warning("Bounded call failed", task=label, error=str(_box["exc"]))
    return _box["result"]


def _duration_seconds(value: Any) -> float | None:
    """Best-effort track duration in seconds (None when unknown/zero)."""
    try:
        v = float(value or 0)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _refresh_album_live_context(album, album_context, track_contexts, album_type_field) -> None:
    if not album_type_field:
        return
    album_context["musicbrainz_album_type"] = str(album_type_field)
    album_context["live_album_type"] = detect_live_album_type(album, album_type_field)
    album_context["is_live_album"] = bool(album_context["live_album_type"])
    for _tc in track_contexts or []:
        _track = _tc.get("track") or {}
        _tc["album_context_live"] = 1 if album_context["is_live_album"] else 0
        _tc["exclude_from_stats"] = should_exclude_track_from_stats(
            title=_tc.get("title") or "",
            album=album,
            is_live=int(_track.get("is_live") or 0),
            album_context_live=_tc["album_context_live"],
            album_type=str(album_type_field),
            duration=_duration_seconds(_track.get("duration")),
        )


def _collapse_album_mb_batch(
    mb_batch: dict[str, dict[str, Any]],
    track_contexts: list[dict[str, Any]],
    current_album: str,
) -> None:
    folder_counts: dict[str, int] = {}
    for _tc in track_contexts or []:
        _fp = str((_tc.get("track") or {}).get("file_path") or "")
        if not _fp:
            continue
        _parts = _fp.replace("\\", "/").rstrip("/").split("/")
        if len(_parts) >= 2 and _parts[-2]:
            _folder = _parts[-2].strip()
            folder_counts[_folder] = folder_counts.get(_folder, 0) + 1
            
    anchor = (
        max(folder_counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        if folder_counts
        else str(current_album or "").strip()
    )

    from collections import Counter
    album_counts: Counter = Counter()
    distinct_albums: list[str] = []
    
    for _meta in (mb_batch or {}).values():
        _album = str((_meta or {}).get("album") or "").strip()
        if not _album:
            continue
        album_counts[_album] += 1
        if _album not in distinct_albums:
            distinct_albums.append(_album)

    canonical = None
    if anchor and distinct_albums:
        from difflib import SequenceMatcher

        def _score(name: str) -> float:
            return SequenceMatcher(None, anchor.lower(), name.lower()).ratio()

        best_score = 0.0
        for _name in distinct_albums:
            _sim = _score(_name)
            _tie = album_counts[_name]
            if (
                _sim > best_score
                or (_sim == best_score and _tie > album_counts.get(canonical, 0))
            ):
                best_score = _sim
                canonical = _name
                
        if best_score < 0.85:
            canonical = None
            
    if not canonical and distinct_albums:
        canonical = max(distinct_albums, key=lambda n: (album_counts[n], len(n)))

    if not canonical:
        return

    for _meta in (mb_batch or {}).values():
        if _meta and str(_meta.get("album") or "").strip():
            _meta["album"] = canonical


def _resolve_scan_type(options: dict[str, Any]) -> str:
    if options.get("metadata_only"):
        return "metadata"
    if options.get("singles_only") or options.get("singles_with_missing_popularity"):
        return "singles"
    if options.get("popularity_only"):
        return "popularity"
    return "combined"


def _album_release_is_old(tracks: list[dict[str, Any]] | None, now=None) -> bool:
    try:
        from helpers.config_helpers import get_feature
        age_months = int(get_feature("old_album_age_months", 48) or 48)
    except Exception:
        age_months = 48
    if age_months <= 0:
        return False
    try:
        years: list[int] = []
        for _t in tracks or []:
            _y = _t.get("year") or _t.get("release_year")
            if _y:
                try:
                    years.append(int(str(_y)[:4]))
                except (TypeError, ValueError):
                    continue
        if not years:
            return False
        _min_year = min(years)
        from datetime import datetime
        _now = now or datetime.now()
        _age_months = (_now.year - _min_year) * 12 + _now.month
        return _age_months >= age_months
    except Exception:
        return False


def _load_mb_single_titles(artist: str) -> set[str]:
    if not artist:
        return set()
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        titles: set[str] = set()
        with _db_session() as session:
            result = session.execute(
                _text(
                    "SELECT title FROM missing_releases "
                    "WHERE LOWER(artist) = LOWER(:artist) AND LOWER(COALESCE(category, '')) = 'single'"
                ),
                {"artist": artist},
            )
            titles.update(str(row[0]).strip().lower() for row in result.fetchall() or [] if row[0])
            
        try:
            from services.popularity.release_cache_service import get_artist_single_titles
            titles |= get_artist_single_titles(artist, source="musicbrainz")
        except Exception:
            pass
        return titles
    except Exception as exc:
        logger.debug("Could not pre-load MB singles", artist=artist, error=str(exc))
        return set()


def _load_discogs_single_titles(artist: str) -> set[str]:
    if not artist:
        return set()
    try:
        from services.popularity.release_cache_service import get_artist_single_titles
        return get_artist_single_titles(artist, source="discogs") or set()
    except Exception as exc:
        logger.debug("Could not pre-load Discogs singles", artist=artist, error=str(exc))
        return set()


def _load_discogs_promo_titles(artist: str) -> set[str]:
    if not artist:
        return set()
    try:
        from services.popularity.release_cache_service import get_artist_promo_titles
        return get_artist_promo_titles(artist, source="discogs") or set()
    except Exception as exc:
        logger.debug("Could not pre-load Discogs promos", artist=artist, error=str(exc))
        return set()


def _close_artist_essential_section(
    artist_name: str | None,
    options: dict[str, Any],
    done: set[str],
    featured_rows: list | None,
) -> tuple[set[str], list | None]:
    if not artist_name or options.get("metadata_only"):
        return done, featured_rows
        
    artist_name = _essential_strip_guest_credit(artist_name) or artist_name
    key = str(artist_name).strip().casefold()
    
    if not key or key in done:
        return done, featured_rows
    if not _essential_playlists_enabled(options):
        return done, featured_rows
        
    try:
        if featured_rows is None:
            featured_rows = _fetch_essential_featured_rows()
        _create_essential_m3u(artist_name, featured_rows=featured_rows)
        done.add(key)
    except Exception as exc:
        logger.debug("Essential collection failed", artist=artist_name, error=str(exc))
        
    return done, featured_rows


def _run_album_cover_detection(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    options: dict[str, Any],
) -> None:
    if not artist or not tracks:
        return
    if (
        options.get("singles_only")
        or options.get("singles_with_missing_popularity")
        or options.get("popularity_only")
    ):
        return
        
    try:
        from helpers.config_helpers import get_feature
        _covers_enabled = bool(get_feature("cover_detection_enabled", True))
    except Exception:
        _covers_enabled = True
        
    if not _covers_enabled:
        return

    try:
        from services.enrichment.cover_detection_service import detect_covers_for_album
        _cover_results = detect_covers_for_album(
            album=album,
            artist=artist,
            tracks=tracks,
            conn=None,
            force=bool(options.get("force")),
        )
        if _cover_results:
            log_unified(f"[COVER_DETECT] {artist} - {album}: {len(_cover_results)} cover(s) found")
    except Exception as exc:
        logger.debug("Cover detection failed", artist=artist, album=album, error=str(exc))


def _artist_top_marked_cutoffs(
    scan_scores: list[float],
    db_scores: list[float],
    top_percentile: float = 0.10,
    medium_percentile: float = 0.20,
    large_catalog_percentile: float | None = None,
    large_catalog_threshold: int = 30,
) -> tuple[float | None, float | None, int, int]:
    all_scores = [float(s) for s in scan_scores if float(s or 0) > 0]
    all_scores.extend(float(s) for s in db_scores if float(s or 0) > 0)
    
    if not all_scores:
        return None, None, 0, 0
        
    if large_catalog_percentile is not None and len(all_scores) > max(1, int(large_catalog_threshold)):
        top_percentile = large_catalog_percentile
        
    all_scores.sort(reverse=True)
    top_n = max(1, math.ceil(len(all_scores) * top_percentile))
    medium_n = max(1, math.ceil(len(all_scores) * medium_percentile))
    top_cutoff = all_scores[min(top_n - 1, len(all_scores) - 1)]
    medium_cutoff = all_scores[min(medium_n - 1, len(all_scores) - 1)]
    
    return top_cutoff, medium_cutoff, top_n, medium_n


def _apply_popularity_marking_bump(album_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for tr in album_results:
        if not bool(tr.get("popularity_marked")) or not bool(tr.get("is_single")):
            continue
        if str(tr.get("single_confidence") or "low") != "medium":
            continue
        if is_instrumental_track_title(str(tr.get("title") or "")):
            tr["popularity_marked"] = False
            continue
            
        tr["single_confidence"] = "high"
        try:
            raw = tr.get("single_sources") or ""
            sources = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
            if not isinstance(sources, list):
                sources = []
        except Exception:
            sources = []
            
        sources = [s for s in sources if isinstance(s, dict) and str(s.get("source") or "") != "popularity_marked"]
        sources.append({"source": "popularity_marked", "matched": True, "confidence": 0.5})
        tr["single_sources"] = json.dumps(sources, default=str)
        log_unified(f"[scan_runner] Popularity marking upgraded '{tr.get('title')}' to high-confidence single (artist top band)")
        
    return album_results


def _compute_global_5star_locked_titles(
    artist: str,
    all_results: list[dict[str, Any]],
    options: dict[str, Any],
) -> set[str]:
    try:
        _sd = get_config().get("single_detection") or {}
        _top_pct = float(_sd.get("global_5star_catalog_top_pct", 0.20) or 0.20)
        _min_raw = float(_sd.get("global_5star_min_raw_score", 60.0) or 60.0)
    except Exception:
        _top_pct, _min_raw = 0.20, 60.0

    eligible: list[dict[str, Any]] = []
    for _tr in all_results:
        if bool(_tr.get("exclude_from_stats")) or bool(_tr.get("is_live")):
            continue
        _raw = float(_tr.get("_raw_combined") or 0)
        if _raw <= 0:
            continue

        _conf = str(_tr.get("single_confidence") or "low").lower()
        _src_raw = _tr.get("single_sources") or ""
        _has_src = False
        try:
            _parsed = json.loads(_src_raw) if isinstance(_src_raw, str) and _src_raw.strip() else _src_raw
            _has_src = any(isinstance(s, dict) and bool(s.get("matched")) for s in (_parsed or []))
        except Exception:
            _has_src = False
            
        _candidate = _conf in ("high", "medium") or _has_src or bool(_tr.get("popularity_marked"))
        if not _candidate:
            continue
        eligible.append((_raw, _tr))

    if not eligible:
        return set()
        
    eligible.sort(key=lambda pair: pair[0], reverse=True)
    n = max(1, math.ceil(len(all_results) * _top_pct))
    
    return {
        str(_tr.get("title") or "").strip().lower()
        for _raw, _tr in eligible[:n]
        if _raw >= _min_raw
    }


def _mark_track_artist_top_band(album_results: list[dict[str, Any]]) -> None:
    try:
        _sd = get_config().get("single_detection") or {}
        _large_catalog_pct = float(_sd.get("artist_top_percentile_large", 0.25) or 0.25)
        _large_catalog_threshold = int(_sd.get("artist_catalog_large_threshold", 30) or 30)
    except Exception:
        _large_catalog_pct, _large_catalog_threshold = 0.25, 30

    by_primary: dict[str, list[float]] = {}
    for _tr in album_results:
        _score = float(_tr.get("popularity_score") or 0)
        if _score <= 0:
            continue
        _primary = strip_featured_artist(str(_tr.get("artist") or ""))
        by_primary.setdefault(_primary, []).append(_score)

    catalogue_cache: dict[str, list[float]] = {}
    for _tr in album_results:
        if bool(_tr.get("exclude_from_stats")) or is_instrumental_track_title(str(_tr.get("title") or "")):
            _tr["popularity_marked"] = False
            continue
            
        _score = float(_tr.get("popularity_score") or 0)
        _primary = strip_featured_artist(str(_tr.get("artist") or ""))
        if not _primary:
            _tr["popularity_marked"] = False
            continue
            
        if _primary not in catalogue_cache:
            catalogue_cache[_primary] = _load_track_artist_scores(_primary)
            
        catalogue = catalogue_cache[_primary]
        mates = list(by_primary.get(_primary) or [])
        _own_idx = next((i for i, s in enumerate(mates) if s == _score), None)
        if _own_idx is not None:
            mates.pop(_own_idx)
            
        _top_cutoff, _medium_cutoff, _, _ = _artist_top_marked_cutoffs(
            mates, catalogue,
            large_catalog_percentile=_large_catalog_pct,
            large_catalog_threshold=_large_catalog_threshold,
        )
        
        if _top_cutoff is None:
            _tr["popularity_marked"] = False
            continue
            
        _top_marked = _score > 0 and _score >= _top_cutoff
        _medium_marked = (
            _score > 0
            and _score >= _medium_cutoff
            and bool(_tr.get("is_single"))
            and str(_tr.get("single_confidence") or "low") == "medium"
        )
        _tr["popularity_marked"] = bool(_top_marked or _medium_marked)


def _load_track_artist_scores(track_artist: str) -> list[float]:
    if not track_artist:
        return []
    primary = strip_featured_artist(track_artist)
    if not primary:
        return []
        
    db_rows: list[tuple[str, str]] = []
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text(
                    "SELECT title, album, final_score FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND final_score > 0"
                ),
                {"artist": primary},
            ).fetchall()
            db_rows = [
                (str(r[1] or ""), float(r[2] or 0))
                for r in rows or []
                if r[2] and not is_bonus_track_title(str(r[0] or ""))
            ]
    except Exception as exc:
        logger.debug("Track artist score load failed", track_artist=track_artist, error=str(exc))
        return []
        
    if not db_rows:
        return []
        
    try:
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        return list(reanchor_scores_to_album_relative(db_rows))
    except Exception as exc:
        logger.debug("Track artist score re-anchor failed", track_artist=track_artist, error=str(exc))
        return [float(s) for _alb, s in db_rows]


def _album_reference_scores(
    album_results: list[dict[str, Any]],
    score_key: str = "popularity_score",
) -> list[float]:
    full = [float(r.get(score_key) or 0) for r in album_results if float(r.get(score_key) or 0) > 0]
    if len(full) < 3:
        return full
        
    eligible = [
        float(r.get(score_key) or 0)
        for r in album_results
        if float(r.get(score_key) or 0) > 0 and not bool(r.get("exclude_from_stats"))
    ]
    
    if len(eligible) >= 3:
        return eligible
    return full


def _apply_album_relative_normalization(
    album_results: list[dict[str, Any]],
    is_compilation: bool = False,
) -> int:
    if is_compilation:
        return _apply_track_artist_relative_normalization(album_results)
        
    raw_scores = _album_reference_scores(album_results, score_key="_raw_combined")
    if len(raw_scores) < 3:
        return 0
        
    changed = 0
    rows: list[dict[str, Any]] = []
    
    for tr in album_results:
        raw = float(tr.get("_raw_combined") or 0)
        if raw <= 0:
            continue
        remapped = apply_album_relative_popularity(raw, raw_scores)
        if abs(remapped - float(tr.get("popularity_score") or 0)) > 0.001:
            tr["popularity_score"] = remapped
            tr["final_score"] = remapped
            tr["popularity_adjusted"] = True
            rows.append(tr)
            changed += 1
            
    if rows:
        _persist_album_relative_scores(rows)
    return changed


def _apply_track_artist_relative_normalization(album_results: list[dict[str, Any]]) -> int:
    changed = 0
    rows: list[dict[str, Any]] = []
    artist_scores_cache: dict[str, list[float]] = {}
    album_raw_scores = _album_reference_scores(album_results, score_key="_raw_combined")
    
    for tr in album_results:
        raw = float(tr.get("_raw_combined") or 0)
        if raw <= 0:
            continue
            
        track_artist = str(tr.get("artist") or "").strip()
        if not track_artist:
            continue
            
        primary = strip_featured_artist(track_artist)
        if primary not in artist_scores_cache:
            artist_scores_cache[primary] = _load_track_artist_scores(primary)
            
        artist_scores = artist_scores_cache[primary]
        if len(artist_scores) >= ALBUM_RELATIVE_MIN_ALBUM_TRACKS:
            remapped = apply_track_artist_relative_popularity(raw, artist_scores)
        else:
            remapped = apply_album_relative_popularity(raw, album_raw_scores)
            
        if abs(remapped - float(tr.get("popularity_score") or 0)) > 0.001:
            tr["popularity_score"] = remapped
            tr["final_score"] = remapped
            tr["popularity_adjusted"] = True
            rows.append(tr)
            changed += 1
            
    if rows:
        _persist_album_relative_scores(rows)
    return changed


def _persist_album_relative_scores(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            for tr in rows:
                tid = str(tr.get("track_id") or "")
                if not tid:
                    continue
                session.execute(
                    _text("UPDATE tracks SET final_score = :s, popularity = :s WHERE id = :id"),
                    {"s": float(tr.get("final_score") or 0), "id": tid},
                )
    except Exception as exc:
        logger.debug("Album-relative score persist failed", error=str(exc))


def _persist_popularity_marking(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            for tr in rows:
                tid = str(tr.get("track_id") or "")
                if not tid:
                    continue
                session.execute(
                    _text(
                        "UPDATE tracks SET popularity_marked = :marked, "
                        "single_confidence = :conf, single_sources = :sources "
                        "WHERE id = :id"
                    ),
                    {
                        "marked": bool(tr.get("popularity_marked")),
                        "conf": str(tr.get("single_confidence") or "low"),
                        "sources": tr.get("single_sources") or "",
                        "id": tid,
                    },
                )
    except Exception as exc:
        logger.debug("Popularity marking persist failed", error=str(exc))


def run_scan(
    *,
    verbose: bool = False,
    resume_from: str | None = None,
    artist_filter: str | None = None,
    album_filter: str | None = None,
    skip_header: bool = False,
    force: bool = False,
    filter_missing: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
    metadata_only: bool = False,
    clear_single_detection_sources: list | None = None,
    stop_progress_file: str | None = None,
    caller_scan_type: str | None = None,
    **extra_kwargs: Any,
) -> dict[str, Any]:

    options = {
        "verbose": verbose,
        "resume_from": resume_from,
        "artist_filter": artist_filter,
        "album_filter": album_filter,
        "skip_header": skip_header,
        "force": force,
        "filter_missing": filter_missing,
        "singles_only": singles_only,
        "singles_with_missing_popularity": singles_with_missing_popularity,
        "popularity_only": popularity_only,
        "metadata_only": metadata_only,
        "clear_single_detection_sources": clear_single_detection_sources,
        "stop_progress_file": stop_progress_file,
        "caller_scan_type": caller_scan_type,
        **extra_kwargs,
    }

    _deferred_persist = DeferredPersistSink()

    update(stage="loading", progress=3, message="Loading scan candidates...")
    albums = load_candidates(options)
    total_albums = len(albums)

    start(total_items=total_albums)

    if not albums:
        if force:
            log_unified(
                "Popularity Scan - No tracks found. No candidate tracks/albums were "
                "loaded from the library — check the library has been imported."
            )
        else:
            log_unified(
                "Popularity Scan - No tracks found. All tracks may already have "
                "popularity data (run in Forced mode to rescan)."
            )
        update(stage="complete", progress=100, message="No albums to scan.", processed=0, total_items=0)
        finish(success=True)
        return {"success": True, "albums_processed": 0, "tracks_processed": 0}

    _banner_artist = str(options.get("artist_filter") or "").strip()
    _banner_album = str(options.get("album_filter") or "").strip()
    if _banner_album:
        _banner_title = f"ALBUM SCAN: {_banner_artist} — {_banner_album}"
    elif _banner_artist:
        _banner_title = f"ARTIST SCAN: {_banner_artist}"
    else:
        _banner_title = "LIBRARY SCAN"
    log_unified("=" * 80)
    log_unified(f"🚀 {_banner_title} ({total_albums} Album(s) Queued)")
    log_unified("=" * 80)

    try:
        from services.popularity.stages.finalise_stage import prune_genre_playlists_for_deletion
        prune_genre_playlists_for_deletion()
    except BaseException as exc:
        logger.debug("Genre playlist prune skipped", error=str(exc))

    albums_processed = 0
    tracks_processed = 0
    skipped_albums = 0
    results: list[dict[str, Any]] = []
    last_checkpoint_artist: str | None = None

    scan_type = _resolve_scan_type(options)

    _singles_pass = bool(
        options.get("singles_only") or options.get("singles_with_missing_popularity")
    )

    _scan_threads = 4
    try:
        from helpers.config_helpers import get_config as _get_cfg_threads
        _scan_threads = int(((_get_cfg_threads().get("popularity") or {}).get("scan_threads") or 4))
    except Exception:
        pass
    _scan_threads = max(1, min(_scan_threads, 8))

    try:
        from helpers.config_helpers import get_track_timeout_seconds
        _track_timeout_seconds = get_track_timeout_seconds()
    except Exception:
        _track_timeout_seconds = 600
    _track_grace_seconds = 60

    try:
        from helpers.config_helpers import get_prefetch_budget_seconds
        _prefetch_budget_seconds = get_prefetch_budget_seconds()
    except Exception:
        _prefetch_budget_seconds = 360

    log_unified(f"[POPULARITY] Scan mode: {scan_type.capitalize()} — {total_albums} album(s) queued")
    if force:
        log_unified("[POPULARITY] Forced mode — album-skip and score-freeze checks are DISABLED")

    if not artist_filter and not album_filter:
        try:
            _letters = []
            for _cand in albums or []:
                _c = str((_cand.get("artist") or " ")[0].upper())
                _c = "#" if not _c.isalpha() else _c
                if not _letters or _letters[-1] != _c:
                    _letters.append(_c)
            if _letters:
                log_unified(f"[POPULARITY] Letter groups queued: {' → '.join(_letters)}")
        except Exception:
            pass

    artist_lf_context_cache: dict[str, dict[str, Any]] = {}
    artist_mb_singles_cache: dict[str, set[str]] = {}
    artist_discogs_singles_cache: dict[str, set[str]] = {}
    artist_discogs_promo_cache: dict[str, set[str]] = {}

    artist_all_tracks: dict[str, list[dict[str, Any]]] = {}
    for _cand in albums or []:
        _cand_artist = str(_cand.get("artist") or "")
        if _cand_artist:
            artist_all_tracks.setdefault(_cand_artist, []).extend(_cand.get("tracks") or [])

    last_prefetch_artist: str | None = None
    prefetched_popularity: dict[str, dict[str, Any]] = {}

    effective_stop_file = stop_progress_file or extra_kwargs.get("progress_file")
    _last_letter: str | None = None
    _last_quarter = 0

    _artist_scan_results: dict[str, list[dict[str, Any]]] = {}
    _per_album_posted_keys: set[tuple[str, str]] = set()
    _artist_5star_locked_titles: dict[str, set[str]] = {}
    _artist_pending_albums: dict[str, list[dict[str, Any]]] = {}
    _artist_db_rows_cache: dict[str, list[Any]] = {}

    # Album-name renames collected during the album loop, applied AFTER the
    # artist section's star-rating finalise (deferral is required — renaming
    # tracks rows mid-loop would make the finalise's ``album = :album``
    # queries find nothing).
    _deferred_album_renames: dict[str, list[dict[str, Any]]] = {}

    def _load_artist_db_scores(artist: str, scanned_titles: set[str]) -> list[float]:
        db_rows: list[tuple[str, str]] = []
        try:
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            if artist not in _artist_db_rows_cache:
                with _db_session() as session:
                    rows = session.execute(
                        _text(
                            "SELECT title, album, final_score FROM tracks "
                            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                            "AND final_score > 0"
                        ),
                        {"artist": artist},
                    ).fetchall()
                _artist_db_rows_cache[artist] = rows or []
            rows = _artist_db_rows_cache[artist]
            db_rows = [
                (str(r[1] or ""), float(r[2] or 0))
                for r in rows
                if r[2]
                and str(r[0] or "").strip().lower() not in scanned_titles
                and not is_bonus_track_title(str(r[0] or ""))
            ]
        except Exception as exc:
            logger.debug("Artist DB score fetch failed", artist=artist, error=str(exc))
            
        try:
            from services.popularity.popularity_math import reanchor_scores_to_album_relative
            db_scores = reanchor_scores_to_album_relative(db_rows)
        except Exception as exc:
            logger.debug("Artist DB score re-anchor failed", artist=artist, error=str(exc))
            db_scores = [float(s) for _alb, s in db_rows]
            
        return db_scores

    _artist_db_listen_cache: dict[str, list[Any]] = {}

    def _load_artist_db_listeners(artist: str, scanned_titles: set[str]) -> list[float]:
        try:
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            if artist not in _artist_db_listen_cache:
                with _db_session() as session:
                    rows = session.execute(
                        _text(
                            "SELECT title, lastfm_listeners FROM tracks "
                            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                            "AND COALESCE(lastfm_listeners, 0) > 0"
                        ),
                        {"artist": artist},
                    ).fetchall()
                _artist_db_listen_cache[artist] = rows or []
            rows = _artist_db_listen_cache[artist]
            return [
                float(r[1] or 0)
                for r in rows
                if float(r[1] or 0) > 0
                and str(r[0] or "").strip().lower() not in scanned_titles
                and not is_bonus_track_title(str(r[0] or ""))
            ]
        except Exception as exc:
            logger.debug("Artist DB listen fetch failed", artist=artist, error=str(exc))
            return []

    def _post_album_stars(
        artist: str,
        album_results: list[dict[str, Any]],
        is_compilation: bool = False,
        is_va_compilation: bool = False,
    ) -> bool:
        if not album_results or metadata_only:
            return False

        try:
            _apply_album_relative_normalization(
                album_results,
                is_compilation=is_va_compilation,
            )
        except Exception as exc:
            logger.debug("Album-relative normalization failed", error=str(exc))

        _artist_results = _artist_scan_results.get(artist, [])
        scan_scores = [
            float(r.get("popularity_score") or 0)
            for r in _artist_results
            if float(r.get("popularity_score") or 0) > 0
            and not bool(r.get("exclude_from_stats"))
        ]
        scanned_titles = {
            str(r.get("title") or "").strip().lower()
            for r in _artist_results
        }
        _db_scores = _load_artist_db_scores(artist, scanned_titles)
        artist_scores = scan_scores + _db_scores

        _locked_set = _artist_5star_locked_titles.get(artist) or set()
        if _locked_set:
            for _tr in album_results:
                if str(_tr.get("title") or "").strip().lower() in _locked_set:
                    _tr["_global_5star_locked"] = True
                    if not bool(_tr.get("exclude_from_stats")) and not bool(_tr.get("is_live")):
                        log_unified(
                            f"[scan_runner] '{_tr.get('title')}' → GLOBAL 5★ LOCKED "
                            f"(raw {float(_tr.get('_raw_combined') or 0):.1f}, catalog top)"
                        )

        try:
            _top_pct = float((get_config().get("single_detection") or {}).get("artist_top_percentile", 0.10) or 0.10)
            _medium_pct = float((get_config().get("single_detection") or {}).get("artist_medium_bump_percentile", 0.20) or 0.20)
            _large_pct = float((get_config().get("single_detection") or {}).get("artist_top_percentile_large", 0.25) or 0.25)
            _large_threshold = int((get_config().get("single_detection") or {}).get("artist_catalog_large_threshold", 30) or 30)
        except Exception:
            _top_pct, _medium_pct, _large_pct, _large_threshold = 0.10, 0.20, 0.25, 30
            
        if is_va_compilation:
            _top_cutoff = _medium_cutoff = None
        else:
            _top_cutoff, _medium_cutoff, _top_n, _medium_n = _artist_top_marked_cutoffs(
                scan_scores, _db_scores,
                top_percentile=_top_pct,
                medium_percentile=_medium_pct,
                large_catalog_percentile=_large_pct,
                large_catalog_threshold=_large_threshold,
            )
            
        if not options.get("popularity_only") and (is_va_compilation or _top_cutoff is not None):
            if is_va_compilation:
                _mark_track_artist_top_band(album_results)
            elif _top_cutoff is not None:
                for _tr in album_results:
                    if bool(_tr.get("exclude_from_stats")) or is_instrumental_track_title(str(_tr.get("title") or "")):
                        _tr["popularity_marked"] = False
                        continue
                    _score = float(_tr.get("popularity_score") or 0)
                    _top_marked = _score >= _top_cutoff and _score > 0
                    _medium_marked = (
                        _score >= _medium_cutoff
                        and _score > 0
                        and bool(_tr.get("is_single"))
                        and str(_tr.get("single_confidence") or "low") == "medium"
                    )
                    _tr["popularity_marked"] = bool(_top_marked or _medium_marked)
                    
            _apply_popularity_marking_bump(album_results)
            try:
                _persist_popularity_marking(album_results)
            except Exception:
                pass

        try:
            _outcome = post_album_star_ratings(
                album_results=album_results,
                artist=artist,
                artist_scores=artist_scores,
                options=options,
            )
            if int(_outcome.get("star_ratings") or 0) > 0:
                _per_album_posted_keys.add((artist, str(album_results[0].get("album") or "")))
                return True
        except Exception as exc:
            logger.debug("Per-album star posting failed", artist=artist, error=str(exc))
        return False

    def _flush_artist_star_ratings(artist: str) -> None:
        pending = _artist_pending_albums.pop(artist, [])
        if not pending or metadata_only:
            return

        try:
            _all_artist_results = _artist_scan_results.get(artist, [])
            if len(_all_artist_results) >= 5:
                _locked_titles = _compute_global_5star_locked_titles(
                    artist,
                    _all_artist_results,
                    options,
                )
                if _locked_titles:
                    _artist_5star_locked_titles[artist] = _locked_titles
                    log_unified(
                        f"[scan_runner] Global 5★ pre-pass: locked {len(_locked_titles)} "
                        f"catalog top track(s) for '{artist}' (order-independent)"
                    )
        except Exception as exc:
            logger.debug("Global 5★ pre-pass failed", artist=artist, error=str(exc))

        for _pending in pending:
            _album_results_this = _pending.get("album_results") or []
            if not _album_results_this:
                continue
            _posted = _post_album_stars(
                artist,
                _album_results_this,
                is_compilation=bool(_pending.get("is_compilation")),
                is_va_compilation=bool(_pending.get("is_va_compilation")),
            )
            if _posted and total_albums <= 1:
                try:
                    from services.popularity.stages.finalise_stage import refresh_genre_playlists_for_album
                    refresh_genre_playlists_for_album(artist, str(_pending.get("album") or ""))
                except Exception as exc:
                    logger.debug("Genre playlist refresh failed", artist=artist, error=str(exc))

    _essential_featured_rows: list | None = None
    _essential_playlists_done: set[str] = set()
    _section_artist: str | None = None

    def _close_artist_section(artist_name: str | None) -> None:
        nonlocal _essential_featured_rows, _essential_playlists_done
        if artist_name:
            try:
                _flush_artist_star_ratings(artist_name)
            except BaseException as exc:
                logger.debug("Deferred star-rating flush failed", artist=artist_name, error=str(exc))

            # Apply the artist's deferred album-name renames NOW — the star
            # ratings have been finalised against the ORIGINAL album names,
            # so renaming the tracks rows afterwards is safe and the
            # cleaned names persist for the next scan.
            try:
                _renames = _deferred_album_renames.pop(artist_name, []) or []
                from services.metadata.album_name_update_service import (
                    apply_album_name_update,
                )
                for _name_update in _renames:
                    try:
                        _old = str(_name_update.get("album") or "")
                        _new = str(_name_update.get("new_name") or "")
                        _res = apply_album_name_update(
                            artist=artist_name,
                            album=_old,
                            new_name=_new,
                        )
                        if _res.get("changed"):
                            log_unified(
                                f"[ALBUM_NAME] '{artist_name} - {_old}' → '{_new}' "
                                f"(reason={_name_update.get('reason')}, "
                                f"db={_res.get('db_updated')}, "
                                f"files={_res.get('files_updated')})"
                            )
                    except Exception as exc:
                        logger.debug(
                            "[ALBUM_NAME] Deferred rename failed",
                            artist=artist_name,
                            album=_name_update.get("album"),
                            error=str(exc),
                        )
            except Exception as exc:
                logger.debug(
                    "[ALBUM_NAME] Deferred rename batch failed",
                    artist=artist_name,
                    error=str(exc),
                )
                
        _essential_playlists_done, _essential_featured_rows = _close_artist_essential_section(
            artist_name,
            options,
            _essential_playlists_done,
            _essential_featured_rows,
        )
        
        if _essential_playlists_done:
            options["_essential_playlists_done"] = _essential_playlists_done
        if _essential_featured_rows is not None:
            options["_essential_featured_rows"] = _essential_featured_rows

    for album_index, album_row in enumerate(albums, start=1):

        if effective_stop_file and is_stop_requested(effective_stop_file):
            _close_artist_section(_section_artist)
            log_unified("Scan stopped by user request")
            finish(success=False)
            return False

        artist = album_row.get("artist") or ""

        if artist and artist != _section_artist:
            _close_artist_section(_section_artist)
            _section_artist = artist

        if artist:
            try:
                _section_albums = [_ar for _ar in albums if (_ar.get("artist") or "") == artist]
                _section_titles: set[str] = set()
                _pre_scores: list[float] = []
                _pre_listens: list[float] = []
                
                for _ar in _section_albums:
                    for _t in (_ar.get("tracks") or []):
                        _t_title = str(_t.get("title") or "").strip().lower()
                        _section_titles.add(_t_title)
                        _score = float(_t.get("final_score") or _t.get("popularity") or _t.get("popularity_score") or 0)
                        if _score > 0:
                            _pre_scores.append(_score)
                        _lf = float(_t.get("lastfm_listeners") or 0)
                        if _lf > 0:
                            _pre_listens.append(_lf)
                            
                _pre_scores += _load_artist_db_scores(artist, _section_titles)
                try:
                    _pre_listens += _load_artist_db_listeners(artist, _section_titles)
                except Exception as exc:
                    logger.debug("Pass-1 listen baseline failed", artist=artist, error=str(exc))
                    
                _pre_primary = strip_featured_artist(artist)
                if len(_pre_scores) >= ALBUM_RELATIVE_MIN_ALBUM_TRACKS:
                    options["artist_stats_override"] = list(_pre_scores)
                else:
                    options.pop("artist_stats_override", None)
                    
                if len(_pre_listens) >= ALBUM_RELATIVE_MIN_ALBUM_TRACKS:
                    options["artist_listen_override"] = list(_pre_listens)
                else:
                    options.pop("artist_listen_override", None)
                    
                log_unified(
                    f"[scan_runner] Pass-1 artist pre-scan: {_pre_primary} — "
                    f"{len(_pre_scores)} catalogue score(s), "
                    f"{len(_pre_listens)} listen(s) pre-loaded for artist_z"
                )
            except Exception as exc:
                logger.debug("Pass-1 artist pre-scan failed", artist=artist, error=str(exc))
                options.pop("artist_stats_override", None)
                options.pop("artist_listen_override", None)

        album = album_row.get("album") or ""
        tracks = album_row.get("tracks") or []
        _album_start = len(results)

        _first = (artist or " ")[0].upper()
        _letter = "#" if not _first.isalpha() else _first
        if _letter != _last_letter:
            _last_letter = _letter
            log_unified(f"Popularity Scan - Letter '{_letter}'")

        log_unified(f"[{album_index}/{total_albums}] Processing: \"{str(album or '').strip()}\" ({len(tracks or [])} Tracks)")

        _is_compilation_artist = _is_comp_artist(artist)

        if artist and artist not in artist_mb_singles_cache and not _is_compilation_artist:
            artist_mb_singles_cache[artist] = _load_mb_single_titles(artist)
        mb_cached_singles = artist_mb_singles_cache.get(artist) or set()

        if artist and artist not in artist_discogs_singles_cache and not _is_compilation_artist:
            artist_discogs_singles_cache[artist] = _load_discogs_single_titles(artist)
        discogs_cached_singles = artist_discogs_singles_cache.get(artist) or set()

        if artist and artist not in artist_discogs_promo_cache and not _is_compilation_artist:
            artist_discogs_promo_cache[artist] = _load_discogs_promo_titles(artist)
        discogs_cached_promos = artist_discogs_promo_cache.get(artist) or set()

        _mode_meta = bool(options.get("metadata_only"))
        _mode_pop = bool(options.get("popularity_only"))
        _mode_singles = bool(options.get("singles_only") or options.get("singles_with_missing_popularity"))
        _album_is_old = _album_release_is_old(tracks)
        skip_album = False
        
        if not force and not album_filter:
            try:
                from helpers.config_helpers import get_feature
                if _mode_meta:
                    skip_days = int(get_feature("metadata_skip_days", 0) or 0)
                    if _album_is_old:
                        skip_days = int(get_feature("metadata_old_album_skip_days", 30) or 0)
                elif _mode_pop:
                    skip_days = int(get_feature("popularity_skip_days", 7) or 0)
                    if _album_is_old:
                        skip_days = int(get_feature("popularity_old_album_skip_days", 30) or 0)
                elif _mode_singles:
                    skip_days = int(get_feature("singles_skip_days", 7) or 0)
                    if _album_is_old:
                        skip_days = int(get_feature("singles_old_album_skip_days", 30) or 0)
                else:
                    skip_days = int(get_feature("album_skip_days", 7) or 0)
                    if _album_is_old:
                        skip_days = int(get_feature("album_old_album_skip_days", 30) or 0)
            except Exception:
                skip_days = 7
                
            if skip_days > 0:
                if was_album_scanned(artist, album, scan_type, skip_days):
                    skip_album = True
                    log_unified(f"Popularity Scan - Skipping album \"{str(album or '').strip()}\" (scanned within last {skip_days} days)")
                elif get_feature("skip_unchanged_albums", True) and tracks and not _mode_meta:
                    if _mode_singles:
                        all_done = all(t.get("single_detection_last_updated") for t in tracks)
                    elif _mode_pop:
                        all_done = all(float(t.get("final_score") or 0) > 0 for t in tracks)
                    else:
                        all_scored = all(float(t.get("final_score") or 0) > 0 for t in tracks)
                        all_assessed = all(t.get("single_detection_last_updated") for t in tracks)
                        all_done = all_scored and all_assessed
                    if all_done:
                        skip_album = True
                        log_unified(f"Popularity Scan - Skipping album \"{str(album or '').strip()}\" (no changes detected)")
                        
        if skip_album:
            skipped_albums += 1
            try:
                from services.popularity.stages.album_stage import ensure_album_type
                _detected_type = ensure_album_type(album_row, options)
            except Exception as exc:
                logger.debug("Album type ensure failed", artist=artist, album=album, error=str(exc))
                _detected_type = None
                
            try:
                from helpers.config_helpers import get_feature as _gf
                _run_singles_on_skip = _gf("run_singles_on_skipped_albums", False)
            except Exception:
                _run_singles_on_skip = False
                
            if _run_singles_on_skip and not metadata_only and not popularity_only:
                try:
                    _album_context, _track_contexts = prepare_tracks_for_album(
                        artist=artist,
                        album=album,
                        tracks=tracks,
                        album_artist=album_row.get("album_artist"),
                        spotify_album_type=album_row.get("spotify_album_type"),
                        musicbrainz_album_type=album_row.get("musicbrainz_album_type"),
                    )
                    _refresh_album_live_context(
                        album,
                        _album_context,
                        _track_contexts,
                        _detected_type or "",
                    )
                    _album_result = {
                        "album_row": album_row,
                        "album_context": _album_context,
                        "detected_album_type": _detected_type or "",
                        "is_heterogeneous": False,
                    }
                    _singles_options = dict(options)
                    _singles_options["singles_detection_only"] = True
                    import concurrent.futures as _futures2
                    _skip_jobs = [(apply_context_fields_to_track(_tc), _tc) for _tc in _track_contexts]

                    def _run_skip_job(job: tuple) -> dict[str, Any] | None:
                        _prepared, _tc = job
                        try:
                            return process_track(
                                track=_prepared,
                                track_context=_tc,
                                album_context=_album_context,
                                album_result=_album_result,
                                options=_singles_options,
                                album_lb_listens=None,
                                artist_max_lf_listeners=0,
                                artist_lf_context={},
                                album_tracks=tracks,
                                mb_cached_singles=mb_cached_singles,
                                discogs_cached_singles=discogs_cached_singles,
                                discogs_cached_promos=discogs_cached_promos,
                                prefetched_popularity={},
                            )
                        except BaseException as _skip_exc:
                            logger.warning("Skip singles worker crashed", artist=artist, album=album, error=str(_skip_exc))
                            return None

                    def _run_skip_job_bounded(job: tuple) -> dict[str, Any] | None:
                        import threading as _th2
                        _skip_box: dict[str, Any] = {"result": None, "exc": None, "done": False}

                        def _skip_work() -> None:
                            try:
                                _skip_box["result"] = _run_skip_job(job)
                            except BaseException as _exc:  # noqa: BLE001
                                _skip_box["exc"] = _exc
                            finally:
                                _skip_box["done"] = True

                        _t2 = _th2.Thread(target=_skip_work, name="skip-worker", daemon=True)
                        _t2.start()
                        _t2.join(timeout=_track_worker_hard_timeout)
                        if not _skip_box["done"]:
                            logger.warning(
                                "Skip singles worker exceeded timeout — abandoning",
                                artist=artist,
                                track=str((job[0] or {}).get("title") or "?"),
                                timeout_seconds=_track_worker_hard_timeout,
                            )
                            return None
                        return _skip_box["result"]

                    def _collect_skip_results(_pool: Any, _skip_futures: list[Any]) -> list[dict[str, Any] | None]:
                        collected: list[dict[str, Any] | None] = [None] * len(_skip_futures)
                        _index_by_future = {_f: _i for _i, _f in enumerate(_skip_futures)}
                        try:
                            for _f in _futures2.as_completed(_skip_futures, timeout=_track_timeout_seconds):
                                _i = _index_by_future[_f]
                                try:
                                    collected[_i] = _f.result()
                                except BaseException as _exc:
                                    logger.warning("Skip singles result failed", artist=artist, album=album, error=str(_exc))
                                    collected[_i] = None
                        except _futures2.TimeoutError:
                            pass
                        _still = [f for f in _skip_futures if not f.done()]
                        if _still:
                            try:
                                for _f in _futures2.as_completed(_still, timeout=_track_grace_seconds):
                                    _i = _index_by_future[_f]
                                    try:
                                        collected[_i] = _f.result()
                                    except BaseException as _exc:
                                        logger.warning("Skip singles result failed (grace)", artist=artist, album=album, error=str(_exc))
                                        collected[_i] = None
                            except _futures2.TimeoutError:
                                pass
                        for _f in _skip_futures:
                            if not _f.done():
                                _i = _index_by_future[_f]
                                logger.warning(
                                    "Skip singles result timed out",
                                    artist=artist, album=album,
                                    timeout_seconds=_track_timeout_seconds,
                                    grace_seconds=_track_grace_seconds,
                                )
                                collected[_i] = None
                        return collected

                    if _scan_threads > 1 and len(_skip_jobs) > 1:
                        _pool = _futures2.ThreadPoolExecutor(max_workers=_scan_threads)
                        try:
                            _skip_futures = [_pool.submit(_run_skip_job_bounded, job) for job in _skip_jobs]
                            _skip_results = _collect_skip_results(_pool, _skip_futures)
                        finally:
                            try:
                                _pool.shutdown(wait=False, cancel_futures=True)
                            except Exception:
                                pass
                    else:
                        _skip_results = [_run_skip_job_bounded(job) for job in _skip_jobs]
                except Exception as exc:
                    logger.debug("Singles-only pass failed", artist=artist, album=album, error=str(exc))
            continue

        progress = 5 + int((album_index / total_albums) * 90)
        current_item = f"{artist} - {album}"

        _progress_cb = options.get("progress_callback")
        if callable(_progress_cb):
            try:
                _progress_cb(album_index, total_albums, current_item)
            except Exception as exc:
                logger.debug("progress_callback failed", error=str(exc))

        if effective_stop_file and artist and artist != last_checkpoint_artist:
            try:
                # The row's scan-type label follows the file being written:
                # a dedicated popularity scan writes "popularity_scan", but
                # when an ORCHESTRATOR drives this runner (dashboard "All"
                # full scan passes stop_progress_file=full_scan) the row is
                # the full-scan row — labelling it "popularity_scan" created
                # the phantom "Popularity Scan 0/?" active marker next to the
                # real "Full Scan" row on the dashboard.
                _row_scan_type = (
                    "full_scan"
                    if effective_stop_file == get_scan_progress_path("full_scan")
                    else "popularity_scan"
                )
                write_progress_with_current_artist(
                    effective_stop_file,
                    _row_scan_type,
                    True,
                    current_artist=artist,
                    extra={"status": "running", "percent_complete": progress, "current_item": current_item},
                )
                if not artist_filter and not album_filter:
                    save_artist_scan_checkpoint(artist, effective_stop_file)
                last_checkpoint_artist = artist
            except Exception as exc:
                logger.debug("Progress checkpoint write failed", error=str(exc))

        if artist and artist not in artist_lf_context_cache and not _is_comp_artist(artist):
            try:
                from services.enrichment.single_detection_context_service import get_artist_lastfm_context
                artist_lf_context_cache[artist] = get_artist_lastfm_context(artist, None, None)
            except Exception as exc:
                logger.debug("Last.fm context fetch failed", artist=artist, error=str(exc))
                artist_lf_context_cache[artist] = {"mean": 0, "stdev": 0, "total": 0, "values": []}
        artist_lf_context = artist_lf_context_cache.get(artist) or {}

        update(
            stage="album",
            progress=progress,
            message=f"Preparing {current_item}",
            current_item=current_item,
            processed=album_index,
            total_items=total_albums,
        )

        try:
            album_context, track_contexts = prepare_tracks_for_album(
                artist=artist,
                album=album,
                tracks=tracks,
                album_artist=album_row.get("album_artist"),
                spotify_album_type=album_row.get("spotify_album_type"),
                musicbrainz_album_type=album_row.get("musicbrainz_album_type"),
            )
            stat_eligible_tracks = get_stat_eligible_tracks(track_contexts)
        except Exception as exc:
            logger.warning("Album prep failed", artist=artist, album=album, error=str(exc))
            try:
                log_unified(f"[POPULARITY] Album '{str(artist or '').strip()} - {str(album or '').strip()}' skipped (prep error: {exc})")
            except Exception:
                pass
            try:
                record_scan(scan_type, "failed", message=f"Album prep failed: {exc}", artist=artist, album=album)
            except Exception:
                pass
            albums_processed += 1
            continue

        try:
            record_scan(scan_type, "started", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)

            _full_pass = not (
                options.get("metadata_only")
                or options.get("popularity_only")
                or options.get("singles_only")
                or options.get("singles_with_missing_popularity")
                or options.get("singles_detection_only")
            )
            if _full_pass:
                options["defer_full_enrichment"] = True

            log_unified(f"[POPULARITY] Enriching album: {artist} - {album}")
            album_result = _bounded_call_result(
                lambda: enrich_album(
                    album_row=album_row,
                    album_context=album_context,
                    stat_eligible_tracks=stat_eligible_tracks,
                    options=options,
                ),
                seconds=_prefetch_budget_seconds,
                label=f"album enrichment '{artist} - {album}'",
                default=None,
            ) or {}
            log_unified(f"[POPULARITY] Album enriched: {artist} - {album} (type={album_result.get('detected_album_type')})")

            _refresh_album_live_context(
                album,
                album_context,
                track_contexts,
                str((album_result or {}).get("detected_album_type") or ""),
            )

            track_dicts = [tc["track"] for tc in track_contexts if tc.get("track")]
            _is_compilation_artist = _is_comp_artist(artist)
        
            if artist and artist != last_prefetch_artist and not _is_compilation_artist:
                last_prefetch_artist = artist
                prefetched_popularity = {}
                _prefetch_state: dict[str, Any] = {"prefetched_popularity": {}}

                def _prefetch_artist_work() -> None:
                    if not _singles_pass:
                        try:
                            from services.popularity.popularity_cache_service import prefetch_artist_popularity
                            _prefetch_state["prefetched_popularity"] = prefetch_artist_popularity(
                                artist=artist,
                                tracks=artist_all_tracks.get(artist) or track_dicts,
                                force=bool(options.get("force")),
                                cache_full_catalogue=True,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Popularity cache prefetch failed (falls back to per-track lookups)",
                                artist=artist, error=str(exc),
                            )

                    try:
                        from services.popularity.release_cache_service import prefetch_artist_releases
                        _discogs_id = ""
                        for _t in artist_all_tracks.get(artist) or track_dicts:
                            _discogs_id = str(_t.get("discogs_artist_id") or "").strip()
                            if _discogs_id:
                                break
                        if not _discogs_id:
                            try:
                                from services.enrichment.discogs_service import DiscogsService
                                from helpers.config_helpers import get_config
                                _tok = ((get_config().get("api_integrations") or {}).get("discogs") or {}).get("token") or ""
                                if _tok and _tok.lower() not in ("your_discogs_token", "your_token", "placeholder"):
                                    _discogs_id = str(DiscogsService(token=_tok).get_artist_id(artist) or "").strip()
                            except Exception as exc:
                                logger.debug("Discogs artist id resolution failed", artist=artist, error=str(exc))
                        prefetch_artist_releases(artist, _discogs_id)
                    except Exception as exc:
                        logger.warning(
                            "Release cache prefetch failed (single-title cache unavailable)",
                            artist=artist, error=str(exc),
                        )

                    if not _is_compilation_artist and not _singles_pass:
                        try:
                            from services.popularity.release_cache_service import (
                                populate_missing_release_tracklists,
                                refresh_missing_releases_for_artist,
                            )
                            refresh_missing_releases_for_artist(artist)
                            populate_missing_release_tracklists(artist, limit=3)
                        except Exception as exc:
                            logger.debug("Missing-releases refresh failed", artist=artist, error=str(exc))

                log_unified(
                    f"[POPULARITY] Prefetching popularity + release data for '{artist}' "
                    f"(budget {int(_prefetch_budget_seconds or 0)}s)"
                )
                _prefetch_start = time.monotonic()
                _bounded_call(_prefetch_artist_work, seconds=_prefetch_budget_seconds, label=f"per-artist prefetch for '{artist}'")
                _prefetch_elapsed = time.monotonic() - _prefetch_start
                prefetched_popularity = _prefetch_state["prefetched_popularity"]
                log_unified(
                    f"[POPULARITY] Prefetch complete for '{artist}' in {_prefetch_elapsed:.1f}s "
                    f"({len(prefetched_popularity or {})} tracks pre-loaded)"
                )

            if not _singles_pass:
                try:
                    from services.popularity.popularity_sources import get_listenbrainz_album_tracklist_with_release
                    _needs_album_lb = bool(options.get("force"))
                    if not _needs_album_lb:
                        for _t in track_dicts:
                            if not _t.get("title"):
                                continue
                            _entry = (prefetched_popularity or {}).get(normalize_for_aggregation(_t["title"])) or {}
                            if _entry.get("source") != "album_tracklist":
                                _needs_album_lb = True
                                break
                    if _needs_album_lb:
                        _album_lb_by_title, _album_release_mbid = _bounded_call_result(
                            lambda: (
                                get_listenbrainz_album_tracklist_with_release(artist, album, track_dicts)
                                or ({}, "")
                            ),
                            seconds=min(_track_timeout_seconds, 120),
                            label=f"album-tracklist LB for '{artist} - {album}'",
                            default=({}, ""),
                        )
                        _cache_rows: list[dict[str, Any]] = []
                        for _t in track_dicts:
                            if not _t.get("title"):
                                continue
                            _key = normalize_for_aggregation(_t["title"])
                            _entry = _album_lb_by_title.get(_key)
                            _cur = prefetched_popularity.setdefault(_key, {})
                            if _entry and _entry.get("listenbrainz_listens"):
                                _cur["listenbrainz_listens"] = int(_entry["listenbrainz_listens"] or 0)
                                _cur["listenbrainz_users"] = int(_entry.get("listenbrainz_users") or 0)
                                _cur["recording_mbid"] = _entry.get("recording_mbid")
                                _cur["_album_tracklist"] = True
                                _cur["source"] = "album_tracklist"
                                log_unified(f"[scan_runner] Album-tracklist LB match for '{_t.get('title')}' ({artist} - {album}): {_cur['listenbrainz_listens']} listens")
                            if _album_release_mbid:
                                _cur["_album_tracklist"] = True
                                _cur["source"] = "album_tracklist"
                                _cache_rows.append({
                                    "artist": artist,
                                    "title": str(_t["title"]),
                                    "lastfm_listeners": int(_cur.get("lastfm_listeners") or 0),
                                    "lastfm_playcount": int(_cur.get("lastfm_playcount") or 0),
                                    "listenbrainz_listens": int(_cur.get("listenbrainz_listens") or 0),
                                    "listenbrainz_users": int(_cur.get("listenbrainz_users") or 0),
                                    "source": "album_tracklist",
                                })
                        if _cache_rows:
                            try:
                                from db.repositories.popularity_cache import upsert_track_popularity_bulk
                                upsert_track_popularity_bulk(_cache_rows)
                            except Exception as exc:
                                logger.debug("Album-tracklist cache persist failed", error=str(exc))
                except Exception as exc:
                    logger.debug("Album-tracklist LB lookup failed", artist=artist, album=album, error=str(exc))

            album_lb_listens: list[int] = []
            for _t in track_dicts:
                _e = (prefetched_popularity or {}).get(normalize_for_aggregation(_t.get("title") or "")) or {}
                _tc = int(_e.get("listenbrainz_listens") or 0)
                if _tc > 0:
                    album_lb_listens.append(_tc)

            artist_max_lf = (
                _bounded_call_result(
                    lambda: get_lastfm_artist_max_listeners(artist),
                    seconds=min(_track_timeout_seconds, 90),
                    label=f"artist max LF listeners for '{artist}'",
                    default=0,
                )
                if not _singles_pass
                else 0
            )

            album_count = len(track_contexts)
            log_unified(f"[POPULARITY] Album {album_index}/{total_albums} ({scan_type}): {artist} - {album} ({album_count} tracks)")

            if not _singles_pass and not options.get("popularity_only"):
                try:
                    from services.enrichment.musicbrainz_service import MusicBrainzService, get_shared_mb_client
                    options["mb_batch_metadata"] = {}
                    _mb_entries: list[tuple[str, str]] = []
                    for _tc in track_contexts:
                        if _tc.get("recording_mbid") or _tc.get("mbid") or _tc.get("musicbrainz_trackid"):
                            continue
                        _tt = _tc.get("title")
                        _aa = _tc.get("artist")
                        if _tt and _aa:
                            _mb_entries.append((str(_tt), str(_aa)))
                    if _mb_entries:
                        _mb_batch = _bounded_call_result(
                            lambda: MusicBrainzService(
                                http_client=get_shared_mb_client()
                            ).lookup_album_metadata(_mb_entries, album=str(album or "")),
                            seconds=min(_track_timeout_seconds, 120),
                            label=f"MB album batch for '{artist} - {album}'",
                            default={},
                        ) or {}
                        if _mb_batch:
                            _collapse_album_mb_batch(_mb_batch, track_contexts, album)
                            options["mb_batch_metadata"] = _mb_batch
                            log_unified(f"[POPULARITY] MusicBrainz batch resolved {len(_mb_batch)}/{len(_mb_entries)} track(s) for {artist} - {album}")
                except Exception as exc:
                    logger.debug("MusicBrainz album batch failed", artist=artist, album=album, error=str(exc))

            if not _singles_pass and not options.get("popularity_only"):
                try:
                    _lb_tag_mbids: list[str] = []
                    for _tc in track_contexts:
                        _t = _tc.get("track") or {}
                        if _t.get("listenbrainz_genres"):
                            continue
                        _m = str(_t.get("recording_mbid") or _t.get("mbid") or _t.get("musicbrainz_trackid") or "").strip()
                        if _m and _m not in _lb_tag_mbids:
                            _lb_tag_mbids.append(_m)
                    for _mb_entry in (options.get("mb_batch_metadata") or {}).values():
                        _m = str((_mb_entry or {}).get("recording_mbid") or "").strip()
                        if _m and _m not in _lb_tag_mbids:
                            _lb_tag_mbids.append(_m)
                    if _lb_tag_mbids:
                        from api_clients.listenbrainz import get_recording_tags_batch
                        options["lb_recording_tags_batch"] = _bounded_call_result(
                            lambda: get_recording_tags_batch(_lb_tag_mbids),
                            seconds=min(_track_timeout_seconds, 90),
                            label=f"LB tag batch for '{artist} - {album}'",
                            default={},
                        ) or {}
                except Exception as exc:
                    logger.debug("LB tag batch failed", artist=artist, album=album, error=str(exc))

            _pop_due = False
            if _mode_singles:
                try:
                    from helpers.config_helpers import get_feature
                    _pop_window = int(get_feature("popularity_skip_days", 7) or 0)
                    if _album_is_old:
                        _pop_window = int(get_feature("popularity_old_album_skip_days", 30) or 0)
                except Exception:
                    _pop_window = 7
                if _pop_window <= 0:
                    _pop_due = True
                else:
                    _pop_scored_recently = (
                        was_album_scanned(artist, album, "popularity", _pop_window)
                        or was_album_scanned(artist, album, "combined", _pop_window)
                    )
                    _pop_due = not _pop_scored_recently

            import concurrent.futures as _futures

            _track_jobs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]] = []
            for track_context in track_contexts:
                prepared_track = apply_context_fields_to_track(track_context)
                _frozen = False
                
                if not options.get("force") and should_freeze_track(prepared_track):
                    _frozen = True
                    logger.debug(
                        "Freezing mature track — running singles/cover/genre only",
                        track=prepared_track.get("title", "?"),
                        existing_score=prepared_track.get("final_score", 0),
                    )
                    if not prepared_track.get("popularity_frozen"):
                        try:
                            from sqlalchemy import text as _text
                            from db.engine import db_session as _db_session
                            with _db_session() as session:
                                session.execute(
                                    _text(
                                        "UPDATE tracks SET popularity_frozen = TRUE, "
                                        "popularity_frozen_at = CURRENT_TIMESTAMP "
                                        "WHERE id = :id AND COALESCE(popularity_frozen, FALSE) = FALSE"
                                    ),
                                    {"id": prepared_track.get("id")},
                                )
                        except Exception as exc:
                            logger.debug("Could not persist freeze flag", track_id=prepared_track.get("id"), error=str(exc))
                            
                _track_options = dict(options)
                _track_options["_deferred_persist"] = _deferred_persist
                _track_options["refresh_popularity_if_due"] = _pop_due
                if _frozen:
                    _track_options["frozen_track"] = True
                _track_jobs.append((prepared_track, track_context, _track_options, _frozen))

            def _run_track_job(job: tuple) -> dict[str, Any] | None:
                _prepared, _tc, _opts, _frozen = job
                try:
                    return process_track(
                        track=_prepared,
                        track_context=_tc,
                        album_context=album_context,
                        album_result=album_result,
                        options=_opts,
                        album_lb_listens=album_lb_listens if album_lb_listens else None,
                        artist_max_lf_listeners=artist_max_lf,
                        artist_lf_context=artist_lf_context,
                        album_tracks=track_dicts,
                        mb_cached_singles=mb_cached_singles,
                        discogs_cached_singles=discogs_cached_singles,
                        discogs_cached_promos=discogs_cached_promos,
                        prefetched_popularity=prefetched_popularity,
                    )
                except BaseException as _track_exc:
                    logger.warning(
                        "Track worker crashed",
                        artist=artist, album=album, track=_prepared.get("title", "?"), error=str(_track_exc)
                    )
                    try:
                        log_unified(f"[TRACK_STAGE] Track worker crashed for '{_prepared.get('title', '?')}' — skipping ({_track_exc})")
                    except Exception:
                        pass
                    return None

            _track_worker_hard_timeout = max(60, min(_track_timeout_seconds, 600))

            def _run_track_job_bounded(job: tuple) -> dict[str, Any] | None:
                import threading as _th
                _box: dict[str, Any] = {"result": None, "exc": None, "done": False}

                def _work() -> None:
                    try:
                        _box["result"] = _run_track_job(job)
                    except BaseException as _exc:  # noqa: BLE001
                        _box["exc"] = _exc
                    finally:
                        _box["done"] = True

                _t = _th.Thread(target=_work, name="track-worker", daemon=True)
                _t.start()
                _t.join(timeout=_track_worker_hard_timeout)
                if not _box["done"]:
                    _title = str((job[0] or {}).get("title") or "?")
                    logger.warning(
                        "Track worker exceeded timeout — abandoning, scan continues",
                        artist=artist, album=album, track=_title, timeout_seconds=_track_worker_hard_timeout
                    )
                    try:
                        log_unified(f"[TRACK_STAGE] '{_title}' exceeded {_track_worker_hard_timeout}s — track skipped for this pass (scan continues)")
                    except Exception:
                        pass
                    return None
                if _box["exc"] is not None:
                    return None
                return _box["result"]

            def _collect_track_results(_pool: Any, _track_futures: list[Any], _job_titles: list[str]) -> list[dict[str, Any] | None]:
                collected: list[dict[str, Any] | None] = [None] * len(_track_futures)
                _index_by_future = {_f: _i for _i, _f in enumerate(_track_futures)}
                _deadline_seconds = _track_timeout_seconds

                def _collect_finished(deadline: float) -> None:
                    try:
                        _remaining = max(0.0, deadline - time.monotonic())
                    except Exception:
                        _remaining = 0.0
                    try:
                        for _f in _futures.as_completed(_track_futures, timeout=_remaining):
                            _i = _index_by_future[_f]
                            try:
                                collected[_i] = _f.result()
                            except BaseException as _exc:
                                logger.warning("Track result collection failed", artist=artist, album=album, error=str(_exc))
                                try:
                                    log_unified(f"[TRACK_STAGE] Track timed out or failed for '{artist} - {album}' — skipping ({_exc})")
                                except Exception:
                                    pass
                                collected[_i] = None
                    except _futures.TimeoutError:
                        pass 

                _phase1_started = time.monotonic()
                _last_heartbeat = _phase1_started
                _heartbeat_interval = 60.0
                _deadline_at = time.monotonic() + _deadline_seconds

                while True:
                    _remaining_phase = max(0.0, _deadline_at - time.monotonic())
                    if _remaining_phase <= 0:
                        break
                    _wait_chunk = min(_remaining_phase, _heartbeat_interval)
                    _collect_finished(time.monotonic() + _wait_chunk)
                    _now_hb = time.monotonic()
                    # FAST PATH: once every future is done, collection is
                    # complete — return immediately.  Previously this loop
                    # kept iterating (and re-entering as_completed with a
                    # fresh timeout) until the FULL album deadline elapsed,
                    # which produced the observed multi-minute stall AFTER
                    # all tracks had already finished (e.g. 8m19s gap between
                    # the last [TRACK] log and the [TRACK_RESULT] logs).
                    if all(_f.done() for _f in _track_futures):
                        break
                    if _now_hb - _last_heartbeat >= _heartbeat_interval:
                        _last_heartbeat = _now_hb
                        _in_flight = [
                            _job_titles[_index_by_future[f]]
                            for f in _track_futures
                            if not f.done() and _index_by_future[f] < len(_job_titles)
                        ]
                        if _in_flight:
                            logger.warning(
                                "Album still waiting on tracks",
                                artist=artist, album=album, 
                                tracks_remaining=len(_in_flight),
                                wait_seconds=int(_now_hb - _phase1_started),
                                in_flight=_in_flight[:8]
                            )
                            try:
                                log_unified(
                                    f"[TRACK_STAGE] '{artist} - {album}' still waiting on "
                                    f"{len(_in_flight)} track(s) after {int(_now_hb - _phase1_started)}s "
                                    f"— in flight: {', '.join(_in_flight[:8])}"
                                )
                            except Exception:
                                pass

                _still_running = [f for f in _track_futures if not f.done()]
                if _still_running:
                    _collect_finished(time.monotonic() + _track_grace_seconds)
                    _still_running = [f for f in _track_futures if not f.done()]

                if _still_running:
                    _dropped_titles = [_job_titles[_index_by_future[f]] for f in _still_running if _index_by_future[f] < len(_job_titles)]
                    logger.warning(
                        "Track result collection failed — dropped tracks",
                        artist=artist, album=album,
                        dropped_count=len(_still_running),
                        total_futures=len(_track_futures),
                        timeout_seconds=_deadline_seconds,
                        grace_seconds=_track_grace_seconds,
                        dropped=_dropped_titles,
                    )
                    try:
                        log_unified(
                            f"[TRACK_STAGE] Track(s) timed out for '{artist} - {album}' "
                            f"after {_deadline_seconds}s (+{_track_grace_seconds}s grace) — "
                            f"skipped: {', '.join(_dropped_titles) or '?'}"
                        )
                    except Exception:
                        pass
                    for _f in _still_running:
                        collected[_index_by_future[_f]] = None
                return collected

            _track_results_ordered: list[dict[str, Any] | None] = []
            _job_titles = [str((job[0] or {}).get("title") or "?") for job in _track_jobs]
            
            if _scan_threads > 1 and len(_track_jobs) > 1:
                _pool = _futures.ThreadPoolExecutor(max_workers=_scan_threads)
                try:
                    _track_futures = []
                    _worker_slots = max(1, min(_scan_threads, 8))
                    _slot_semaphore = threading.BoundedSemaphore(_worker_slots)

                    def _submit_chunked(job):
                        _slot_semaphore.acquire()
                        try:
                            return _run_track_job_bounded(job)
                        finally:
                            _slot_semaphore.release()

                    _track_futures = [_pool.submit(_submit_chunked, job) for job in _track_jobs]
                    _track_results_ordered = _collect_track_results(_pool, _track_futures, _job_titles)
                finally:
                    try:
                        _pool.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
            else:
                _track_results_ordered = [_run_track_job_bounded(job) for job in _track_jobs]

            try:
                _deferred_payloads = _deferred_persist.drain()
                if _deferred_payloads:
                    upsert_tracks_bulk(_deferred_payloads)
                    logger.debug("Bulk-persisted track(s)", count=len(_deferred_payloads), artist=artist, album=album)
            except Exception as exc:
                logger.warning("Bulk track persist failed", artist=artist, album=album, error=str(exc))
                
            try:
                _deferred_payloads = _deferred_persist.drain()
                if _deferred_payloads:
                    upsert_tracks_bulk(_deferred_payloads)
                    logger.debug("Bulk-persisted late track(s)", count=len(_deferred_payloads), artist=artist, album=album)
            except Exception as exc:
                logger.warning("Late bulk track persist failed", artist=artist, album=album, error=str(exc))

            for _track_i, ((_prepared, _tc, _opts, _frozen), track_result) in enumerate(zip(_track_jobs, _track_results_ordered)):
                if track_result is not None:
                    results.append(track_result)
                    if not options.get("metadata_only") and isinstance(track_result, dict):
                        _tt = _prepared.get("title", "Unknown Track")
                        _fs = track_result.get("popularity_score")
                        if _frozen:
                            log_unified(f"[TRACK_RESULT] '{_tt}' -> Final: {float(_fs or 0.0):.1f} (frozen | LF: {float(track_result.get('lastfm_score') or 0.0):.1f} | LB: {float(track_result.get('listenbrainz_score') or 0.0):.1f})")
                        else:
                            log_unified(f"[TRACK_RESULT] '{_tt}' -> Final: {float(_fs or 0.0):.1f} (LF: {float(track_result.get('lastfm_score') or 0.0):.1f} | LB: {float(track_result.get('listenbrainz_score') or 0.0):.1f})")
                tracks_processed += 1

                # ── Per-track progress heartbeat ───────────────────────────
                # The album-level ``_progress_cb`` fires once per album (at
                # album start), so a long rate-limited album leaves the footer
                # frozen for minutes.  Fire it per track too — the full-scan
                # orchestrator uses ``current_item`` to render a live
                # "now scanning: <track>" line, and ``track_fraction`` lets it
                # advance the percentage WITHIN the album band instead of
                # waiting for the next album boundary.
                try:
                    _track_denom = max(1, len(_track_jobs) - 1)
                    _track_frac = (_track_i / _track_denom) if len(_track_jobs) > 1 else 1.0
                    _track_item = f"{current_item} — {_prepared.get('title', '?')}"
                    if callable(_progress_cb):
                        try:
                            _progress_cb(album_index, total_albums, _track_item, _track_frac)
                        except Exception:
                            pass
                    _track_progress = min(100, progress + int(_track_frac * (90 / max(1, total_albums))))
                    update(
                        stage="album",
                        progress=_track_progress,
                        message=f"Processing {_prepared.get('title', '?')}",
                        current_item=_track_item,
                        processed=album_index,
                        total_items=total_albums,
                    )
                except Exception:
                    pass

            _track_failure_ratio = (
                (len(_track_jobs) - sum(1 for r in _track_results_ordered if r is not None))
                / max(1, len(_track_jobs))
                if _track_jobs else 0.0
            )

            if _full_pass:
                def _post_singles_enrichment_work() -> None:
                    try:
                        from services.popularity.stages.album_stage import enrich_album_extras
                        _extra_ctx, _extra_similar, _extra_meta = enrich_album_extras(
                            artist=artist,
                            album=album,
                            album_context=album_context,
                            album_tracks=tracks,
                            detected_type=str((album_result or {}).get("detected_album_type") or ""),
                            options=options,
                        )
                        if album_result is not None:
                            album_result.setdefault("album_context", {}).update(_extra_ctx)
                            album_result["similar_artists"] = _extra_similar
                            album_result["artist_metadata"] = _extra_meta
                    except Exception as exc:
                        logger.debug("Post-singles enrichment failed", artist=artist, album=album, error=str(exc))

                if _track_failure_ratio >= 0.5:
                    logger.warning(
                        "Skipping post-singles enrichment — track workers stalled (resource exhaustion guard)",
                        artist=artist, album=album, failure_ratio_pct=_track_failure_ratio * 100
                    )
                else:
                    log_unified(
                        f"[POPULARITY] Post-singles enrichment for '{artist} - {album}' "
                        f"(covers, genres, artist metadata)"
                    )
                    _bounded_call(
                        _post_singles_enrichment_work,
                        seconds=_prefetch_budget_seconds,
                        label=f"post-singles enrichment for '{artist} - {album}'",
                    )

            if _track_failure_ratio < 0.5:
                _run_album_cover_detection(
                    artist=artist,
                    album=album,
                    tracks=tracks,
                    options=options,
                )
            else:
                logger.debug("Skipping cover detection — track workers stalled (resource exhaustion guard)", artist=artist, album=album)

            if _mode_meta or _full_pass:
                try:
                    from services.metadata.album_tag_sync_service import sync_album_file_tags
                    _tag_sync = sync_album_file_tags(artist=artist, album=album)
                    if _tag_sync and (_tag_sync.get("files_updated") or _tag_sync.get("corrections_recorded")):
                        log_unified(
                            f"[ALBUM_TAG_SYNC] {artist} - {album}: filled "
                            f"{_tag_sync.get('files_updated', 0)} file(s), recorded "
                            f"{_tag_sync.get('corrections_recorded', 0)} correction(s)"
                            f"{' (perfect MB match)' if _tag_sync.get('perfect_match') else ''}"
                        )
                except Exception as exc:
                    logger.debug("Album tag sync failed", artist=artist, album=album, error=str(exc))

            # ── Album-name cleaning (metadata_update config) ──────────────
            # The user's request: with "missing releases" and singles the
            # album name should be cleaned up during the Popularity Scan —
            # repeated edition markers ("(reissue) (reissue) (reissue)",
            # "(10 Year Anniversary)" x4) are stripped, optionally using the
            # MusicBrainz release title, and the result is written to the DB
            # (always) and the audio file tags (when configured).
            #
            # IMPORTANT: only the RESOLUTION runs here — the DB + file write
            # is deferred until the artist section closes.  Applying the
            # rename immediately would rename the tracks rows BEFORE the
            # artist-section star-rating finalise (``_flush_artist_star_
            # ratings``) queries them by ``album = :album`` — the queries
            # would find 0 rows and the album would never get its ★ ratings.
            # Singles-only passes never finalise albums the same way and
            # skip windows make repeated cleaning wasteful — skipped.
            if not _mode_singles:
                try:
                    from services.metadata.album_name_update_service import (
                        resolve_album_name,
                    )
                    _new_name, _reason = resolve_album_name(
                        artist=artist,
                        album=album,
                    )
                    if _reason and _new_name and _new_name != album:
                        _deferred_album_renames.setdefault(artist, []).append({
                            "album": album,
                            "new_name": _new_name,
                            "reason": _reason,
                        })
                except Exception as exc:
                    logger.debug(
                        "[ALBUM_NAME] Cleaning skipped",
                        artist=artist,
                        album=album,
                        error=str(exc),
                    )

            try:
                record_scan(scan_type, "completed", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)
            except Exception:
                pass 

            _album_results_this = results[_album_start:]
            if _album_results_this:
                _artist_scan_results.setdefault(artist, []).extend(_album_results_this)
                _artist_pending_albums.setdefault(artist, []).append({
                    "album_results": _album_results_this,
                    "is_compilation": bool(album_context.get("is_compilation")),
                    "is_va_compilation": bool(album_context.get("is_va_compilation")),
                    "album": album,
                })

        except BaseException as _album_exc:
            try:
                _deferred_payloads = _deferred_persist.drain()
                if _deferred_payloads:
                    upsert_tracks_bulk(_deferred_payloads)
                    logger.debug("Safety-flushed deferred track(s)", count=len(_deferred_payloads), artist=artist, album=album)
            except Exception:
                pass
                
            _album_error = _album_exc
            try:
                _exc_type = type(_album_exc)
                _is_future_like = (
                    hasattr(_album_exc, "exception")
                    and callable(getattr(_album_exc, "exception", None))
                    and hasattr(_album_exc, "result")
                    and callable(getattr(_album_exc, "result", None))
                )
                logger.warning(
                    "Album failed",
                    artist=artist, album=album,
                    exception_type=f"{_exc_type.__module__}.{_exc_type.__name__}",
                    future_like=_is_future_like,
                )
                if _is_future_like:
                    _inner = _album_exc.exception()
                    if _inner is not None:
                        _album_error = _inner
                    else:
                        _result = _album_exc.result()
                        _album_error = (
                            RuntimeError(f"Album future completed with a value instead of raising — result type: {type(_result).__name__}")
                            if _result is not None
                            else RuntimeError("Album future completed with None instead of raising")
                        )
            except Exception:
                pass
                
            logger.warning("Album failed completely", artist=artist, album=album, error=str(_album_error))
            try:
                log_unified(f"[POPULARITY] Album '{artist} - {album}' failed ({_album_error})")
            except Exception:
                pass
            try:
                record_scan(scan_type, "failed", message=f"Album failed: {_album_error}", artist=artist, album=album)
            except Exception:
                pass
                
        albums_processed += 1

        _quarter = (albums_processed * 4) // total_albums
        if _quarter > _last_quarter:
            _last_quarter = _quarter
            log_unified(f"[POPULARITY] {_quarter * 25}% complete ({albums_processed}/{total_albums} albums processed)")

    if tracks_processed == 0:
        log_unified(
            "Popularity Scan - All albums were skipped (recently scanned or up to "
            "date). Run in Forced mode to rescan."
        )

    _close_artist_section(_section_artist)

    update(stage="finalising", progress=98, message="Finalising popularity scan...", processed=total_albums, total_items=total_albums)

    if not metadata_only:
        if _per_album_posted_keys:
            options["_per_album_posted"] = True
            options["_per_album_posted_keys"] = _per_album_posted_keys
        try:
            finalise_scan(results=results, options=options)
        except BaseException as _finalise_exc:
            logger.warning("Finalise failed", error=str(_finalise_exc))
            try:
                log_unified(f"[POPULARITY] Finalise step failed ({_finalise_exc})")
            except Exception:
                pass
    else:
        try:
            from services.popularity.stages.finalise_stage import _create_genre_top_track_playlists
            _genre_playlists_written = _create_genre_top_track_playlists()
            if _genre_playlists_written:
                log_unified(f"[FINALISE_STAGE] Genre playlists: {_genre_playlists_written} file(s) written")
        except Exception as exc:
            logger.debug("Metadata genre playlist rebuild failed", error=str(exc))

    update(stage="complete", progress=100, message="Popularity scan complete.", processed=total_albums, total_items=total_albums)
    finish(success=True)

    return {
        "success": True,
        "albums_processed": albums_processed,
        "albums_skipped": skipped_albums,
        "tracks_processed": tracks_processed,
    }
