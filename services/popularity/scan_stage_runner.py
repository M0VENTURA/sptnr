"""Staged popularity scan runner."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from helpers.logging_config import log_unified
from services.popularity.progress_tracker import finish, start, update
from services.popularity.popularity_cache_policy import should_freeze_track
from services.popularity.scan_hooks import (
    apply_context_fields_to_track,
    get_stat_eligible_tracks,
    prepare_tracks_for_album,
)
from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_math import (
    apply_album_relative_popularity,
    apply_track_artist_relative_popularity,
)
from services.popularity.popularity_sources import (
    get_lastfm_artist_max_listeners,
)
from services.popularity.stages.album_stage import enrich_album
from services.popularity.stages.finalise_stage import finalise_scan, post_album_star_ratings
from services.popularity.stages.load_stage import load_candidates
from services.popularity.stages.track_stage import process_track
from services.scanning.scan_state import (
    is_stop_requested,
    save_artist_scan_checkpoint,
    write_progress_with_current_artist,
)
from services.scanning.scan_history_service import record_scan, was_album_scanned

logger = logging.getLogger(__name__)


def _resolve_scan_type(options: dict[str, Any]) -> str:
    """Return a human-readable scan-type label from the runner options."""
    if options.get("metadata_only"):
        return "metadata"
    if options.get("singles_only") or options.get("singles_with_missing_popularity"):
        return "singles"
    if options.get("popularity_only"):
        return "popularity"
    return "combined"


def _load_mb_single_titles(artist: str) -> set[str]:
    """Return MusicBrainz single titles cached in ``missing_releases``.

    Mirrors the legacy pre-load: singles that MusicBrainz knows about but that
    may not be in the user's library yet. These are used to confirm single
    status without a per-track MusicBrainz API call.
    """
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
        # Known MusicBrainz singles/EPs from the artist release cache (prefetched
        # once per artist — see release_cache_service).
        try:
            from services.popularity.release_cache_service import get_artist_single_titles
            titles |= get_artist_single_titles(artist, source="musicbrainz")
        except Exception:
            pass
        return titles
    except Exception as exc:
        logger.debug("[scan_runner] Could not pre-load MB singles for '%s': %s", artist, exc)
        return set()


def _load_discogs_single_titles(artist: str) -> set[str]:
    """Return Discogs single/EP titles from the artist release cache.

    Populated once per artist by ``prefetch_artist_releases`` (one Discogs
    artist-releases call); lets singles detection match local tracks against
    known Discogs singles without per-track Discogs searches.
    """
    if not artist:
        return set()
    try:
        from services.popularity.release_cache_service import get_artist_single_titles
        return get_artist_single_titles(artist, source="discogs") or set()
    except Exception as exc:
        logger.debug("[scan_runner] Could not pre-load Discogs singles for '%s': %s", artist, exc)
        return set()


def _load_discogs_promo_titles(artist: str) -> set[str]:
    """Return Discogs promo single/EP titles from the artist release cache.

    Promo-only releases confirm a track was issued as a (promotional) single,
    but they are weaker evidence than a commercial single — detection treats a
    promo-only Discogs match as a medium-confidence source.
    """
    if not artist:
        return set()
    try:
        from services.popularity.release_cache_service import get_artist_promo_titles
        return get_artist_promo_titles(artist, source="discogs") or set()
    except Exception as exc:
        logger.debug("[scan_runner] Could not pre-load Discogs promos for '%s': %s", artist, exc)
        return set()


def _run_album_cover_detection(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    options: dict[str, Any],
) -> None:
    """Run the full CoverDetector pass for ONE album after per-track detection.

    Runs AFTER the per-track singles/cover detection loop so it never blocks
    the album enrichment section (album art caching / artist metadata). Uses
    the full pipeline (ISRC → MusicBrainz cover relations → writer analysis
    → heuristics → work-history fallback), resolves the ORIGINAL artist and
    renames confirmed covers to "Title (Artist Cover)" in the DB and file
    tags (legacy parity — same behaviour the album stage previously ran
    serially right after art caching).

    Skipped for singles / popularity-only passes; disable via
    ``features.cover_detection_enabled``.
    """
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

    conn = None
    try:
        from db.utils import get_db_connection
        from services.enrichment.cover_detection_service import detect_covers_for_album
        conn = get_db_connection()
        _cover_results = detect_covers_for_album(
            album=album,
            artist=artist,
            tracks=tracks,
            conn=conn,
            force=bool(options.get("force")),
        )
        if _cover_results:
            logger.info(
                "[COVER_DETECT] %s - %s: %d cover(s) found",
                artist, album, len(_cover_results),
            )
    except Exception as exc:
        logger.debug(
            "[scan_runner] Cover detection failed for '%s - %s': %s",
            artist, album, exc,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _artist_top_marked_cutoff(scan_scores: list[float], db_scores: list[float]) -> tuple[float | None, int]:
    """Return ``(cutoff_score, top_n)`` for the artist's top-10% marking.

    The artist's catalogue = this scan's results so far + stored DB scores.
    ``top_n = ceil(total * 0.10)``; a track whose popularity score is at or
    above the ``top_n``-th score is ``popularity_marked``.  Returns
    ``(None, 0)`` when there is no score data to rank.
    """
    all_scores = [float(s) for s in scan_scores if float(s or 0) > 0]
    all_scores.extend(float(s) for s in db_scores if float(s or 0) > 0)
    if not all_scores:
        return None, 0
    all_scores.sort(reverse=True)
    top_n = max(1, math.ceil(len(all_scores) * 0.10))
    cutoff = all_scores[min(top_n - 1, len(all_scores) - 1)]
    return cutoff, top_n


def _apply_popularity_marking_bump(album_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upgrade medium-confidence singles that are ``popularity_marked`` to high.

    Spec rule 3: a track detected as a MEDIUM-confidence single whose popularity
    score is in the artist's top 10% becomes HIGH confidence.  The flag is
    surfaced in ``single_sources`` (``popularity_marked``) so the track page's
    source table shows WHY, and the star-rating pass sees the upgraded
    confidence.  Returns the (possibly mutated) list.
    """
    for tr in album_results:
        if not bool(tr.get("popularity_marked")):
            continue
        if not bool(tr.get("is_single")):
            continue
        if str(tr.get("single_confidence") or "low") != "medium":
            continue
        tr["single_confidence"] = "high"
        try:
            raw = tr.get("single_sources") or ""
            sources = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
            if not isinstance(sources, list):
                sources = []
        except Exception:
            sources = []
        sources = [
            s for s in sources
            if isinstance(s, dict) and str(s.get("source") or "") != "popularity_marked"
        ]
        sources.append({"source": "popularity_marked", "matched": True, "confidence": 0.5})
        tr["single_sources"] = json.dumps(sources, default=str)
        logger.info(
            "[scan_runner] Popularity marking upgraded '%s' to high-confidence single (top 10%% of catalogue)",
            tr.get("title"),
        )
    return album_results


def _load_track_artist_scores(track_artist: str) -> list[float]:
    """Stored popularity scores for a track artist's own catalogue.

    The reference is the artist's OWN albums (``album_artist == artist``),
    never the compilation track itself (which is stored under the
    compilation's album artist).  Used to re-map a compilation track's raw
    popularity relative to its track artist instead of the compilation album.
    """
    if not track_artist:
        return []
    try:
        from services.popularity.popularity_stats_service import calculate_artist_stats
        _, _, values = calculate_artist_stats(None, track_artist)
        return [float(v) for v in (values or []) if float(v or 0) > 0]
    except Exception as exc:
        logger.debug("[scan_runner] Track artist score load failed for %s: %s", track_artist, exc)
        return []


def _apply_album_relative_normalization(
    album_results: list[dict[str, Any]],
    is_compilation: bool = False,
) -> int:
    """Re-map freshly-scored album tracks onto the relative 0-100 scale.

    Regular albums: album-relative only (spec rule 1) — the album's freshly
    computed RAW combined scores are re-mapped via
    ``apply_album_relative_popularity`` (album median + scaled-MAD, robust
    z → 0-100) so scores spread within the album and never clump on the
    ceiling.  Artist-wide stats are ignored.

    Compilation / Various-Artists albums: every track has a different artist,
    so the album distribution (the "album artist" reference) is meaningless.
    Each track is instead re-mapped against ITS OWN track artist's catalogue
    distribution via ``apply_track_artist_relative_popularity`` — the score
    answers "how popular is this track within its artist's discography".

    Tracks without a fresh raw score (cached/frozen tracks carrying a stored
    score, or singles-only passes that never scored) are left untouched.
    Returns the number of tracks re-mapped.
    """
    if is_compilation:
        return _apply_track_artist_relative_normalization(album_results)
    raw_scores = [
        float(r.get("_raw_combined") or 0)
        for r in album_results
        if float(r.get("_raw_combined") or 0) > 0
    ]
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
    """Re-map compilation tracks onto their track artist's 0-100 scale.

    Each compilation track is re-mapped relative to ITS OWN track artist's
    stored catalogue popularity (median + scaled-MAD via
    ``apply_track_artist_relative_popularity``) — not the compilation album's
    distribution.  Artists without enough stored catalogue scores keep their
    raw score.  Returns the number of tracks re-mapped.
    """
    changed = 0
    rows: list[dict[str, Any]] = []
    artist_scores_cache: dict[str, list[float]] = {}
    for tr in album_results:
        raw = float(tr.get("_raw_combined") or 0)
        if raw <= 0:
            continue
        track_artist = str(tr.get("artist") or "").strip()
        if not track_artist:
            continue
        if track_artist not in artist_scores_cache:
            artist_scores_cache[track_artist] = _load_track_artist_scores(track_artist)
        artist_scores = artist_scores_cache[track_artist]
        remapped = apply_track_artist_relative_popularity(raw, artist_scores)
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
    """Persist the album-relative re-mapped popularity scores for one album."""
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
        logger.debug("[scan_runner] Album-relative score persist failed: %s", exc)


def _persist_popularity_marking(rows: list[dict[str, Any]]) -> None:
    """Persist ``popularity_marked`` + bumped single status for one album's tracks."""
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
        logger.debug("[scan_runner] Popularity marking persist failed: %s", exc)


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

    update(stage="loading", progress=3, message="Loading scan candidates...")
    albums = load_candidates(options)
    total_albums = len(albums)

    # Mark the in-memory tracker as running so the WebUI progress service
    # (which only merges stage detail when ``running`` is True) picks up the
    # live stage updates emitted below.
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

    albums_processed = 0
    tracks_processed = 0
    skipped_albums = 0
    results: list[dict[str, Any]] = []
    last_checkpoint_artist: str | None = None

    # Resolved once up-front (used for history records and skip checks).
    scan_type = _resolve_scan_type(options)

    # A singles pass only does singles detection — the popularity API prefetch,
    # ListenBrainz album-tracklist fallback and download gap-detection are not
    # singles work and are skipped (per-track popularity is fetched only for
    # tracks that have no stored data, and only because singles detection's
    # z-score/top-50% gates need SOME score signal).
    _singles_pass = bool(
        options.get("singles_only") or options.get("singles_with_missing_popularity")
    )

    # Surface the scan mode up-front so the unified log says what is running
    # (metadata / popularity / singles / combined) and how much is queued.
    log_unified(f"[POPULARITY] Scan mode: {scan_type.capitalize()} — {total_albums} album(s) queued")
    if force:
        log_unified("[POPULARITY] Forced mode — album-skip and score-freeze checks are DISABLED")

    # Per-artist Last.fm listener-context cache (used for dynamic weighting).
    artist_lf_context_cache: dict[str, dict[str, Any]] = {}

    # Per-artist MB single-title cache (from missing_releases) used to confirm
    # singles without per-track MusicBrainz API calls.
    artist_mb_singles_cache: dict[str, set[str]] = {}

    # Per-artist Discogs single-title cache (from the artist release cache).
    artist_discogs_singles_cache: dict[str, set[str]] = {}

    # Per-artist Discogs promo-title cache — promo-only releases are real
    # confirmation the track was issued as a (promotional) single, but a promo
    # is weaker evidence than a commercial single and is capped at medium
    # confidence during singles detection.
    artist_discogs_promo_cache: dict[str, set[str]] = {}

    # All candidate tracks grouped by artist — the popularity cache prefetch
    # runs ONCE per artist (one getTopTracks + LB batches for the whole
    # catalogue) instead of per album, so the per-track loop makes no
    # popularity API calls.
    artist_all_tracks: dict[str, list[dict[str, Any]]] = {}
    for _cand in albums or []:
        _cand_artist = str(_cand.get("artist") or "")
        if _cand_artist:
            artist_all_tracks.setdefault(_cand_artist, []).extend(_cand.get("tracks") or [])

    last_prefetch_artist: str | None = None
    prefetched_popularity: dict[str, dict[str, Any]] = {}

    # Resolve stop progress file — accept both stop_progress_file (direct)
    # and progress_file (passed via **extra_kwargs by pipeline)
    effective_stop_file = stop_progress_file or extra_kwargs.get("progress_file")

    # Letter progression headers (legacy parity): a full-library scan logs
    # each letter group (#-9, A, B, ...) as it advances, so operators can
    # follow progress through the alphabet in the unified log.
    _last_letter: str | None = None

    # Album-completion quarter tracking (25/50/75/100% progress logs).
    _last_quarter = 0

    # ── Per-album star-rating posting (legacy parity) ───────────────────
    # The legacy scanner assigned, persisted and logged each album's star
    # ratings right after the album completed.  The staged runner previously
    # deferred ALL of that to finalise_scan at the end of the run, so a full
    # artist scan showed no per-album star output until everything finished.
    # We track each artist's accumulated results + existing DB scores so the
    # per-album z-score context matches the end-of-scan pass, and set
    # ``_per_album_posted`` so finalise_scan skips the (now redundant) per-album
    # work and only does artist_stats / playlists / summary.
    _artist_scan_results: dict[str, list[dict[str, Any]]] = {}
    _per_album_posted_keys: set[tuple[str, str]] = set()

    def _load_artist_db_scores(artist: str, scanned_titles: set[str]) -> list[float]:
        """Stored artist scores, EXCLUDING tracks scored during this scan.

        Tracks scored in the current scan were persisted to the DB, so adding
        them back would double-count every scanned track (raw scan score +
        stored adjusted final_score) and drift the artist distribution.  The
        album-level merge in ``post_album_star_ratings`` already excludes
        scanned titles; this is the artist-wide equivalent.
        """
        db_scores: list[float] = []
        try:
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            with _db_session() as session:
                rows = session.execute(
                    _text(
                        "SELECT title, final_score FROM tracks "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                        "AND final_score > 0"
                    ),
                    {"artist": artist},
                ).fetchall()
                db_scores = [
                    float(r[1])
                    for r in rows or []
                    if r[1] and str(r[0] or "").strip().lower() not in scanned_titles
                ]
        except Exception as exc:
            logger.debug("[scan_runner] Artist DB score fetch failed for %s: %s", artist, exc)
        return db_scores

    def _post_album_stars(
        artist: str,
        album_results: list[dict[str, Any]],
        is_compilation: bool = False,
    ) -> bool:
        """Assign/persist/log/sync star ratings for ONE completed album.

        Returns True only when star ratings were actually assigned/persisted —
        the album key is then recorded so the end-of-scan finalise skips it.
        Albums that failed to post stay un-recorded so finalise still handles
        them (no star rating is silently lost).

        Metadata-only passes never rate (scores aren't computed).  Popularity-
        only passes DO rate: they score purely on popularity and can award 5★
        to standout popularity tracks.
        """
        if not album_results or metadata_only:
            return False

        # ── Relative popularity (spec step 1) ─────────────────────────────
        # The final popularity score is the robust-z re-map of the freshly
        # computed raw scores.  This MUST run before the artist score
        # distribution and star ratings so they all see the relative scale.
        # Regular albums re-map against the ALBUM's distribution; compilation /
        # Various-Artists albums re-map each track against its OWN track
        # artist's catalogue instead (the compilation's distribution — the
        # "album artist" reference — is meaningless when every track has a
        # different artist).  Singles-only passes carry no fresh raw scores
        # and are skipped (self-guarded by the helper).
        try:
            _apply_album_relative_normalization(
                album_results,
                is_compilation=is_compilation,
            )
        except Exception as exc:
            logger.debug("[scan_runner] Album-relative normalization failed: %s", exc)

        _artist_results = _artist_scan_results.get(artist, [])
        scan_scores = [
            float(r.get("popularity_score") or 0)
            for r in _artist_results
            if float(r.get("popularity_score") or 0) > 0
        ]
        scanned_titles = {
            str(r.get("title") or "").strip().lower()
            for r in _artist_results
        }
        _db_scores = _load_artist_db_scores(artist, scanned_titles)
        artist_scores = scan_scores + _db_scores

        # ── Artist top-10% popularity marking + medium→high bump ─────────
        # Spec steps 3-4: mark the top 10% of the artist's catalogue by
        # popularity (``popularity_marked``), then upgrade any MEDIUM-confidence
        # single in that range to HIGH.  Both are persisted before star ratings
        # run so the 5★ bump (step 5) sees the upgraded confidence.
        _cutoff, _top_n = _artist_top_marked_cutoff(scan_scores, _db_scores)
        if _cutoff is not None and not options.get("popularity_only"):
            for _tr in album_results:
                _tr["popularity_marked"] = bool(
                    (float(_tr.get("popularity_score") or 0) >= _cutoff)
                    and float(_tr.get("popularity_score") or 0) > 0
                )
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
            logger.debug("[scan_runner] Per-album star posting failed for %s: %s", artist, exc)
        return False

    for album_index, album_row in enumerate(albums, start=1):

        # ✅ Graceful stop support
        if effective_stop_file and is_stop_requested(effective_stop_file):
            logger.info("Scan stopped by user request")
            finish(success=False)
            return False

        artist = album_row.get("artist") or ""
        album = album_row.get("album") or ""
        tracks = album_row.get("tracks") or []

        # Marker into the accumulated results so this album's result dicts can
        # be posted individually once the album completes (see skip + normal
        # paths below).
        _album_start = len(results)

        # Letter-section header (fires once per letter group).
        _first = (artist or " ")[0].upper()
        _letter = "#" if not _first.isalpha() else _first
        if _letter != _last_letter:
            _last_letter = _letter
            log_unified(f"Popularity Scan - Letter '{_letter}'")

        # ── Per-artist singles-title caches (loaded early so the skip path
        #    can also run singles detection) ───────────────────────────────
        _is_compilation_artist = artist.lower() in (
            "various artists", "various artists -", "various",
            "compilation", "soundtrack"
        )
        if artist and artist not in artist_mb_singles_cache and not _is_compilation_artist:
            artist_mb_singles_cache[artist] = _load_mb_single_titles(artist)
        mb_cached_singles = artist_mb_singles_cache.get(artist) or set()

        if artist and artist not in artist_discogs_singles_cache and not _is_compilation_artist:
            artist_discogs_singles_cache[artist] = _load_discogs_single_titles(artist)
        discogs_cached_singles = artist_discogs_singles_cache.get(artist) or set()

        if artist and artist not in artist_discogs_promo_cache and not _is_compilation_artist:
            artist_discogs_promo_cache[artist] = _load_discogs_promo_titles(artist)
        discogs_cached_promos = artist_discogs_promo_cache.get(artist) or set()

        # ── Album skip (album_skip_days + skip-if-unchanged) ────────────
        # Mirrors the legacy scanner: albums already scanned within the
        # configured window, or whose tracks are all scored + singles
        # assessed, are skipped unless forced or explicitly targeted via
        # album_filter (a single-album scan always processes).
        # NOTE: artist-filtered scans (artist page / full library scan) DO
        # honour the timestamp skip — non-forced runs are incremental and
        # only re-process albums that changed or were never scored.
        skip_album = False
        if not force and not album_filter and not metadata_only:
            try:
                from helpers.config_helpers import get_feature
                skip_days = int(get_feature("album_skip_days", 7) or 0)
            except Exception:
                skip_days = 7
            if skip_days > 0:
                if was_album_scanned(artist, album, scan_type, skip_days):
                    skip_album = True
                    log_unified(f"Popularity Scan - Skipping album \"{album}\" (scanned within last {skip_days} days)")
                elif tracks:
                    all_scored = all(float(t.get("final_score") or 0) > 0 for t in tracks)
                    all_assessed = all(t.get("single_detection_last_updated") for t in tracks)
                    if all_scored and all_assessed:
                        skip_album = True
                        log_unified(f"Popularity Scan - Skipping album \"{album}\" (no changes detected)")
        if skip_album:
            skipped_albums += 1
            # Skipped albums still get a lightweight album-type pass so the
            # combined scan (re)sets album types even when the per-track
            # popularity work is skipped (legacy parity). Reuses stored
            # verdicts; only albums missing a type hit MusicBrainz.
            try:
                from services.popularity.stages.album_stage import ensure_album_type
                _detected_type = ensure_album_type(album_row, options)
            except Exception as exc:
                logger.debug("[scan_runner] Album type ensure failed for %s - %s: %s", artist, album, exc)
                _detected_type = None

            # Singles detection still runs for the album (legacy parity):
            # skipping the popularity re-fetch must not suppress the per-album
            # singles output.  A singles-only pass is run so results are
            # emitted after each album even when the album is otherwise up to
            # date.
            if not metadata_only and not popularity_only:
                try:
                    _album_context, _track_contexts = prepare_tracks_for_album(
                        artist=artist,
                        album=album,
                        tracks=tracks,
                        album_artist=album_row.get("album_artist"),
                        spotify_album_type=album_row.get("spotify_album_type"),
                        musicbrainz_album_type=album_row.get("musicbrainz_album_type"),
                    )
                    _album_result = {
                        "album_row": album_row,
                        "album_context": _album_context,
                        "detected_album_type": _detected_type or "",
                        "is_heterogeneous": False,
                    }
                    _singles_options = dict(options)
                    _singles_options["singles_detection_only"] = True
                    for _tc in _track_contexts:
                        _prepared = apply_context_fields_to_track(_tc)
                        _tr = process_track(
                            track=_prepared,
                            track_context=_tc,
                            album_context=_album_context,
                            album_result=_album_result,
                            options=_singles_options,
                            album_lb_listens=None,
                            artist_max_lf_listeners=0,
                            artist_lf_context={},
                            mb_cached_singles=mb_cached_singles,
                            discogs_cached_singles=discogs_cached_singles,
                            discogs_cached_promos=discogs_cached_promos,
                            prefetched_popularity={},
                        )
                        if _tr is not None:
                            results.append(_tr)
                            tracks_processed += 1
                    # Post this (skipped) album's star ratings right away so the
                    # per-album "Star Ratings - Album ..." summary appears as the
                    # scan passes it (legacy parity), carrying stored scores.
                    _album_skip_results = results[_album_start:]
                    if _album_skip_results:
                        _artist_scan_results.setdefault(artist, []).extend(_album_skip_results)
                        _post_album_stars(
                            artist,
                            _album_skip_results,
                            is_compilation=bool(_album_context.get("is_compilation")),
                        )
                except Exception as exc:
                    logger.debug("[scan_runner] Singles-only pass failed for %s - %s: %s", artist, album, exc)
            continue

        # ── Per-artist progress checkpoint ───────────────────────────────
        # Mirrors the legacy scanner: persist an in-progress checkpoint once
        # per artist so an interrupted scan can resume from this point.
        # NOTE: the progress write must NOT include stop_requested=False —
        # that would wipe a dashboard stop request before the loop's next
        # stop check ever runs.
        if effective_stop_file and artist and artist != last_checkpoint_artist:
            try:
                write_progress_with_current_artist(
                    effective_stop_file,
                    "popularity_scan",
                    True,
                    current_artist=artist,
                    extra={"status": "running"},
                )
                # Only full-library scans persist resume checkpoints —
                # targeted artist/album scans must not move the resume point.
                if not artist_filter and not album_filter:
                    save_artist_scan_checkpoint(artist, effective_stop_file)
                last_checkpoint_artist = artist
            except Exception as exc:
                logger.debug("[scan_runner] Progress checkpoint write failed: %s", exc)

        # ── Check if this is a compilation/Various Artists album ──────────
        _is_compilation_artist = artist.lower() in (
            "various artists", "various artists -", "various", 
            "compilation", "soundtrack"
        )

        # ── Per-artist Last.fm listener context (dynamic weight) ────────
        if artist and artist not in artist_lf_context_cache and not _is_compilation_artist:
            try:
                from services.enrichment.single_detection_context_service import get_artist_lastfm_context
                artist_lf_context_cache[artist] = get_artist_lastfm_context(artist, None, None)
            except Exception as exc:
                logger.debug("[scan_runner] Last.fm context fetch failed for %s: %s", artist, exc)
                artist_lf_context_cache[artist] = {"mean": 0, "stdev": 0, "total": 0, "values": []}
        artist_lf_context = artist_lf_context_cache.get(artist) or {}

        progress = 5 + int((album_index / total_albums) * 90)

        update(
            stage="album",
            progress=progress,
            message=f"Preparing {artist} - {album}",
            current_item=f"{artist} - {album}",
            processed=album_index,
            total_items=total_albums,
        )

        album_context, track_contexts = prepare_tracks_for_album(
            artist=artist,
            album=album,
            tracks=tracks,
            album_artist=album_row.get("album_artist"),
            spotify_album_type=album_row.get("spotify_album_type"),
            musicbrainz_album_type=album_row.get("musicbrainz_album_type"),
        )

        stat_eligible_tracks = get_stat_eligible_tracks(track_contexts)

        # Determine actual scan type from options for history display
        record_scan(scan_type, "started", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)

        album_result = enrich_album(
            album_row=album_row,
            album_context=album_context,
            stat_eligible_tracks=stat_eligible_tracks,
            options=options,
        )

        # ── Bulk popularity cache prefetch — once per ARTIST ──────────────
        # Pulls Last.fm (artist.getTopTracks) and ListenBrainz (batches) for
        # the artist's ENTIRE catalogue into track_popularity_cache with a
        # handful of API calls, so the per-track loop makes no popularity
        # calls.  All albums of the same artist reuse the same map, and
        # subsequent scans make ZERO calls (fresh cache rows are reused).
        # Forced scans always recheck.
        #
        # Skip prefetch for compilation/Various Artists albums — the "artist"
        # is not a real artist, so prefetching would waste API calls on
        # irrelevant data. Track-level lookups will still work per-track.
        # A singles pass skips the prefetch entirely: it is not singles work,
        # and track_stage only fetches popularity for tracks missing stored
        # data (needed to drive singles detection's z-scores / top-50% gate).
        track_dicts = [tc["track"] for tc in track_contexts if tc.get("track")]
        _is_compilation_artist = artist.lower() in (
            "various artists", "various artists -", "various", 
            "compilation", "soundtrack"
        )
        
        if artist and artist != last_prefetch_artist and not _is_compilation_artist:
            last_prefetch_artist = artist
            prefetched_popularity = {}
            if not _singles_pass:
                try:
                    from services.popularity.popularity_cache_service import prefetch_artist_popularity
                    prefetched_popularity = prefetch_artist_popularity(
                        artist=artist,
                        tracks=artist_all_tracks.get(artist) or track_dicts,
                        force=bool(options.get("force")),
                        # Album-scoped scans (no cached data for the artist yet)
                        # still persist the artist's full top-tracks catalogue in
                        # one bulk call, so later scans never need per-track calls.
                        cache_full_catalogue=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "[scan_runner] Popularity cache prefetch failed for %s: %s (falls back to per-track lookups)",
                        artist, exc,
                    )

            # ── Artist release cache (albums/EPs/singles) ────────────────
            # One MusicBrainz + one Discogs call per artist fills
            # artist_release_cache; singles detection then matches local
            # tracks against it instead of per-track API searches.
            try:
                from services.popularity.release_cache_service import prefetch_artist_releases
                _discogs_id = ""
                for _t in artist_all_tracks.get(artist) or track_dicts:
                    _discogs_id = str(_t.get("discogs_artist_id") or "").strip()
                    if _discogs_id:
                        break
                if not _discogs_id:
                    # First-ever scan: ``enrich_album`` persists the Discogs
                    # artist id AFTER the in-memory track dicts were loaded, so
                    # it is not in them yet. Resolve it here so the Discogs
                    # release cache (and hence Discogs single confirmation) is
                    # populated on the first pass instead of a week later.
                    try:
                        from services.enrichment.discogs_service import DiscogsService
                        from helpers.config_helpers import get_config
                        _tok = ((get_config().get("api_integrations") or {}).get("discogs") or {}).get("token") or ""
                        if _tok and _tok.lower() not in ("your_discogs_token", "your_token", "placeholder"):
                            _discogs_id = str(DiscogsService(token=_tok).get_artist_id(artist) or "").strip()
                    except Exception as exc:
                        logger.debug("[scan_runner] Discogs artist id resolution failed for %s: %s", artist, exc)
                prefetch_artist_releases(artist, _discogs_id)
            except Exception as exc:
                logger.warning(
                    "[scan_runner] Release cache prefetch failed for %s: %s (single-title cache unavailable)",
                    artist, exc,
                )

            # ── Missing-releases gap detection + tracklists (cache-driven) ─
            # Compares the cached releases against the library (title + year),
            # persists gaps into missing_releases, and caches tracklists for
            # a few of them so they can be queued for download (legacy parity).
            # Not singles work — a singles pass skips it (the download queue is
            # served by dedicated missing-releases scans).
            if not _is_compilation_artist and not _singles_pass:
                try:
                    from services.popularity.release_cache_service import (
                        populate_missing_release_tracklists,
                        refresh_missing_releases_for_artist,
                    )
                    refresh_missing_releases_for_artist(artist)
                    populate_missing_release_tracklists(artist, limit=3)
                except Exception as exc:
                    logger.debug("[scan_runner] Missing-releases refresh failed for %s: %s", artist, exc)

        # ── Album-tracklist ListenBrainz fallback ───────────────────────────
        # Tracks without a resolved recording MBID get no LB data from the
        # per-MBID batch.  ListenBrainz lists albums with their tracks, so
        # pull the album's tracklist + per-track popularity (resolving the
        # release via MB search when the local tracks lack a release MBID)
        # and match the local tracks by normalized title.  The pulled rows
        # are persisted to track_popularity_cache so later scans reuse them
        # without any API calls.  Not singles work — a singles pass skips it.
        if not _singles_pass:
            try:
                from services.popularity.popularity_sources import get_listenbrainz_album_tracklist
                _missing_lb_tracks = [
                    t for t in track_dicts
                    if t.get("title")
                    and not (prefetched_popularity.get(normalize_for_aggregation(t["title"])) or {}).get("listenbrainz_listens")
                ]
                if _missing_lb_tracks or bool(options.get("force")):
                    _album_lb_by_title = get_listenbrainz_album_tracklist(artist, album, track_dicts) or {}
                    _cache_rows: list[dict[str, Any]] = []
                    # Apply the album values to ALL of the album's tracks — the
                    # album tracklist is authoritative for per-track counts (it
                    # matches the ListenBrainz album page), so it overrides any
                    # cached value that was resolved from a different recording.
                    for _t in track_dicts:
                        if not _t.get("title"):
                            continue
                        _key = normalize_for_aggregation(_t["title"])
                        _entry = _album_lb_by_title.get(_key)
                        if _entry and _entry.get("listenbrainz_listens"):
                            _cur = prefetched_popularity.setdefault(_key, {})
                            _cur["listenbrainz_listens"] = int(_entry["listenbrainz_listens"] or 0)
                            _cur["listenbrainz_users"] = int(_entry.get("listenbrainz_users") or 0)
                            _cur["recording_mbid"] = _entry.get("recording_mbid")
                            # Freshly fetched during THIS scan — authoritative even
                            # on forced scans (which normally bypass the cache).
                            _cur["_album_tracklist"] = True
                            _cache_rows.append({
                                "artist": artist,
                                "title": str(_t["title"]),
                                "lastfm_listeners": int(_cur.get("lastfm_listeners") or 0),
                                "lastfm_playcount": int(_cur.get("lastfm_playcount") or 0),
                                "listenbrainz_listens": _cur["listenbrainz_listens"],
                                "listenbrainz_users": _cur["listenbrainz_users"],
                                "source": "album_tracklist",
                            })
                            logger.info(
                                "[scan_runner] Album-tracklist LB match for '%s' (%s - %s): %s listens",
                                _t.get("title"), artist, album, _cur["listenbrainz_listens"],
                            )
                    if _cache_rows:
                        try:
                            from db.repositories.popularity_cache import upsert_track_popularity_bulk
                            upsert_track_popularity_bulk(_cache_rows)
                        except Exception as exc:
                            logger.debug("[scan_runner] Album-tracklist cache persist failed: %s", exc)
            except Exception as exc:
                logger.debug("[scan_runner] Album-tracklist LB fallback failed for %s - %s: %s", artist, album, exc)

        # Album-level LB listen counts (percentile anchor) — only the
        # CURRENT album's tracks anchor the album percentile, even though
        # the prefetched map covers the whole artist catalogue.
        album_lb_listens: list[int] = []
        for _t in track_dicts:
            _e = (prefetched_popularity or {}).get(normalize_for_aggregation(_t.get("title") or "")) or {}
            _tc = int(_e.get("listenbrainz_listens") or 0)
            if _tc > 0:
                album_lb_listens.append(_tc)

        # ── Pre-fetch Last.fm artist peak listener count ──────────────────
        # Used to normalise each track's LF score relative to the artist's
        # most popular track.  Cached in-memory so repeated artist lookups
        # across multiple albums cost at most one API call per artist.
        # A singles pass skips it — it is only needed to score freshly-fetched
        # popularity, and the log-scale fallback covers the rare missing-data
        # track that a singles pass has to score for detection context.
        artist_max_lf = get_lastfm_artist_max_listeners(artist) if not _singles_pass else 0

        # ✅ FIXED: Now properly counting track_contexts instead of the empty album_context array
        album_count = len(track_contexts)
        log_unified(f"[POPULARITY] Album {album_index}/{total_albums} ({scan_type}): {artist} - {album} ({album_count} tracks)")

        for track_context in track_contexts:
            prepared_track = apply_context_fields_to_track(track_context)

            # ── Mature-track freeze ──────────────────────────────────────
            # Tracks older than 2 years with an existing final_score skip the
            # popularity API re-fetch — their popularity is stable.  However,
            # singles detection, cover detection, genre aggregation and star
            # rating still run (legacy parity): the freeze only reuses the
            # cached popularity score, it does NOT skip the track entirely.
            # Forced scans never freeze (legacy ``if not (FORCE_RESCAN or force)``).
            if not options.get("force") and should_freeze_track(prepared_track):
                logger.debug(
                    "[scan_runner] Freezing mature track '%s' (has existing score %.1f) — running singles/cover/genre only",
                    prepared_track.get("title", "?"),
                    prepared_track.get("final_score", 0),
                )
                # Persist the freeze state so the flag survives restarts
                # (legacy behaviour: popularity_frozen = TRUE).
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
                        logger.debug(
                            "[scan_runner] Could not persist freeze flag for %s: %s",
                            prepared_track.get("id"),
                            exc,
                        )
                # Reuse the cached popularity score but still run the rest of
                # the per-track pipeline (metadata/cover/singles/genre).
                frozen_options = dict(options)
                frozen_options["frozen_track"] = True
                frozen_result = process_track(
                    track=prepared_track,
                    track_context=track_context,
                    album_context=album_context,
                    album_result=album_result,
                    options=frozen_options,
                    album_lb_listens=album_lb_listens if album_lb_listens else None,
                    artist_max_lf_listeners=artist_max_lf,
                    artist_lf_context=artist_lf_context,
                    mb_cached_singles=mb_cached_singles,
                    discogs_cached_singles=discogs_cached_singles,
                    discogs_cached_promos=discogs_cached_promos,
                    prefetched_popularity=prefetched_popularity,
                )
                if frozen_result is not None:
                    results.append(frozen_result)
                tracks_processed += 1
                continue

            track_result = process_track(
                track=prepared_track,
                track_context=track_context,
                album_context=album_context,
                album_result=album_result,
                options=options,
                album_lb_listens=album_lb_listens if album_lb_listens else None,
                artist_max_lf_listeners=artist_max_lf,
                artist_lf_context=artist_lf_context,
                mb_cached_singles=mb_cached_singles,
                discogs_cached_singles=discogs_cached_singles,
                discogs_cached_promos=discogs_cached_promos,
                prefetched_popularity=prefetched_popularity,
            )

            if track_result is not None:
                results.append(track_result)

                # Per-track score logging so the dashboard unified log shows
                # exactly how each track was scored (SP / LF / LB / final).
                if isinstance(track_result, dict):
                    title = prepared_track.get("title", "Unknown Track")
                    f_score = track_result.get("popularity_score")
                    sp = track_result.get("spotify_score")
                    lf = track_result.get("lastfm_score")
                    lb = track_result.get("listenbrainz_score")
                    logger.info(
                        "[TRACK_RESULT] '%s' -> Final: %.1f (SP: %.1f | LF: %.1f | LB: %.1f)",
                        title,
                        float(f_score or 0.0),
                        float(sp or 0.0),
                        float(lf or 0.0),
                        float(lb or 0.0),
                    )

            tracks_processed += 1

        # ── Full cover detection stage (after per-track singles/cover
        #    detection) ────────────────────────────────────────────────
        # The per-track ``detect_cover_song`` check above only matches
        # title/composer patterns. This album-level pass runs the full
        # pipeline (ISRC → MusicBrainz cover relations → writer analysis →
        # heuristics → work-history fallback), resolves the ORIGINAL artist
        # and renames confirmed covers to "Title (Artist Cover)" in the DB
        # and file tags. Run AFTER singles detection so the album enrichment
        # section (art caching / artist metadata) never blocks on serial
        # cover API lookups.
        _run_album_cover_detection(
            artist=artist,
            album=album,
            tracks=tracks,
            options=options,
        )

        # Record completion for this album scan
        try:
            record_scan(scan_type, "completed", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)
        except Exception:
            pass  # Non-critical — dashboard data is best-effort

        # ── Per-album star rating posting ────────────────────────────────
        # Assign, persist, log and sync THIS album's star ratings right after
        # it completes (legacy parity) instead of batching them for the end of
        # the scan.  For a full artist scan each album's "Star Ratings - Album"
        # summary appears as the album finishes.
        _album_results_this = results[_album_start:]
        if _album_results_this:
            _artist_scan_results.setdefault(artist, []).extend(_album_results_this)
            _post_album_stars(
                artist,
                _album_results_this,
                is_compilation=bool(album_context.get("is_compilation")),
            )

        albums_processed += 1

        # ── Album-level progress quarters ────────────────────────────────
        # Log 25/50/75/100% as albums complete, so the unified log shows
        # overall scan progress (not just "Album N/M").
        _quarter = (albums_processed * 4) // total_albums
        if _quarter > _last_quarter:
            _last_quarter = _quarter
            log_unified(
                f"[POPULARITY] {_quarter * 25}% complete ({albums_processed}/{total_albums} albums processed)"
            )

    # Nothing was processed (all albums skipped): surface it so the missing
    # finalise output is explainable, not silent.
    if tracks_processed == 0:
        log_unified(
            "Popularity Scan - All albums were skipped (recently scanned or up to "
            "date). Run in Forced mode to rescan."
        )

    update(stage="finalising", progress=98, message="Finalising popularity scan...", processed=total_albums, total_items=total_albums)

    # Star-rating assignment, Navidrome rating sync and NSP playlist creation
    # run on full / singles / popularity-only passes (legacy parity).  Only
    # metadata-only passes skip ratings — scores haven't been computed yet, so
    # every track would incorrectly get 1★.  A popularity-only pass rates on
    # popularity alone (5★ reserved for standout popularity tracks).
    if not metadata_only:
        # When per-album star ratings were posted during the scan loop, tell
        # finalise to skip those albums (it still does artist_stats, the
        # essential playlist and the summary).  Only albums that were actually
        # posted are recorded, so a failed post is never silently dropped.
        if _per_album_posted_keys:
            options["_per_album_posted"] = True
            options["_per_album_posted_keys"] = _per_album_posted_keys
        finalise_scan(results=results, options=options)

    update(stage="complete", progress=100, message="Popularity scan complete.", processed=total_albums, total_items=total_albums)

    finish(success=True)

    return {
        "success": True,
        "albums_processed": albums_processed,
        "albums_skipped": skipped_albums,
        "tracks_processed": tracks_processed,
    }
