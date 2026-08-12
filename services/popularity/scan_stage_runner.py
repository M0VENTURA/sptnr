"""Staged popularity scan runner."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

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
from services.popularity.stages.finalise_stage import finalise_scan, post_album_star_ratings
from services.popularity.stages.load_stage import load_candidates
from services.popularity.stages.track_stage import process_track
from services.scanning.scan_state import (
    is_stop_requested,
    save_artist_scan_checkpoint,
    write_progress_with_current_artist,
)
from services.scanning.scan_history_service import record_scan, was_album_scanned
from services.catalog.album_classification_service import (
    detect_live_album_type,
    is_bonus_track_title,
    should_exclude_track_from_stats,
)

logger = logging.getLogger(__name__)


def _refresh_album_live_context(album, album_context, track_contexts, album_type_field) -> None:
    """Apply the album TYPE as the authoritative live-album signal.

    Live-album detection uses the album type field when the album is matched
    (MusicBrainz/Spotify) and only falls back to title heuristics for
    unmatched albums.  Enrichment determines the type AFTER the album context
    is prepared (title-based), so refresh both in-memory contexts so this
    scan's singles pass sees the authoritative verdict.
    """
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
        )


def _collapse_album_mb_batch(
    mb_batch: dict[str, dict[str, Any]],
    track_contexts: list[dict[str, Any]],
    current_album: str,
) -> None:
    """Rewrite every entry of an album's MB batch to ONE canonical album name.

    The per-track MusicBrainz lookup resolves each track to whichever release
    the recording is listed under FIRST — for multi-edition albums ("OPVS
    NOIR Vol. 3" vs "OPVS NOIR Vol. 3 (Instrumental)") different tracks of the
    SAME folder can therefore resolve to different release titles.  Each track
    then writes its own name to the ``album`` column, splitting one folder into
    several albums on every metadata scan.

    The folder name from the tracks' file paths is authoritative: the canonical
    album name is the batch entry that best matches that folder (ties broken by
    how often MB returned it).  With no folder anchor, the most frequent batch
    name wins.  Every entry is rewritten in place so the whole album agrees.
    """
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
        if best_score < 0.6:
            canonical = None
    if not canonical and distinct_albums:
        canonical = max(distinct_albums, key=lambda n: (album_counts[n], len(n)))

    if not canonical:
        return

    for _meta in (mb_batch or {}).values():
        if _meta and str(_meta.get("album") or "").strip():
            _meta["album"] = canonical


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
            log_unified(
                f"[COVER_DETECT] {artist} - {album}: {len(_cover_results)} cover(s) found",
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


def _artist_top_marked_cutoffs(
    scan_scores: list[float],
    db_scores: list[float],
    top_percentile: float = 0.10,
    medium_percentile: float = 0.20,
    large_catalog_percentile: float | None = None,
    large_catalog_threshold: int = 30,
) -> tuple[float | None, float | None, int, int]:
    """Return ``(top_cutoff, medium_cutoff, top_n, medium_n)`` for marking.

    The artist's catalogue = this scan's results so far + stored DB scores.

    - ``top_n = ceil(total * top_percentile)`` (default 10%): a track at or
      above the ``top_n``-th score is ``popularity_marked`` and earns 5★
      WITHOUT needing any single-detection source (spec rule 2).
    - ``medium_n = ceil(total * medium_percentile)`` (default 20%): a track at
      or above the ``medium_n``-th score that carries a MEDIUM-confidence
      single source is also marked, which bumps it to HIGH confidence → 5★
      (spec rule 3).  The widening only applies when a medium detection source
      exists — popularity alone below the top band never marks.
    - ``large_catalog_percentile`` (optional): when the artist's scored
      catalogue exceeds ``large_catalog_threshold`` tracks (default 30), this
      WIDER top band replaces ``top_percentile`` — a large catalogue needs a
      higher fraction to capture its genuinely popular tracks.  Artists at or
      below the threshold keep ``top_percentile``.

    Returns ``(None, None, 0, 0)`` when there is no score data to rank.
    """
    all_scores = [float(s) for s in scan_scores if float(s or 0) > 0]
    all_scores.extend(float(s) for s in db_scores if float(s or 0) > 0)
    if not all_scores:
        return None, None, 0, 0
    if (
        large_catalog_percentile is not None
        and len(all_scores) > max(1, int(large_catalog_threshold))
    ):
        top_percentile = large_catalog_percentile
    all_scores.sort(reverse=True)
    top_n = max(1, math.ceil(len(all_scores) * top_percentile))
    medium_n = max(1, math.ceil(len(all_scores) * medium_percentile))
    top_cutoff = all_scores[min(top_n - 1, len(all_scores) - 1)]
    medium_cutoff = all_scores[min(medium_n - 1, len(all_scores) - 1)]
    return top_cutoff, medium_cutoff, top_n, medium_n


def _apply_popularity_marking_bump(album_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upgrade medium-confidence singles that are ``popularity_marked`` to high.

    Spec rule 3: a track detected as a MEDIUM-confidence single whose popularity
    score is in the artist's top band (top 10%, or the widened top-20% band for
    medium-source tracks) becomes HIGH confidence.  The flag is surfaced in
    ``single_sources`` (``popularity_marked``) so the track page's source table
    shows WHY, and the star-rating pass sees the upgraded confidence.  Returns
    the (possibly mutated) list.
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
        log_unified(
            f"[scan_runner] Popularity marking upgraded '{tr.get('title')}' to high-confidence single (artist top band)",
        )
    return album_results


def _mark_track_artist_top_band(album_results: list[dict[str, Any]]) -> None:
    """Set ``popularity_marked`` per TRACK ARTIST on a VA-compilation album.

    A VA compilation's album artist ("Various Artists") has no real catalogue,
    so the album-level top-% cutoffs would rank one track against every other
    compilation track across the library.  Each track is instead ranked
    against ITS OWN track artist's stored catalogue — the same re-anchored
    scores used for track-artist normalization, so both sides are on the
    album-relative scale.  A track in its artist's top 10% (or the widened
    top-20% band as a medium-confidence single) is ``popularity_marked`` and
    can earn 5★ organically — a genuine monster hit on a soundtrack (e.g. a
    lead single) marks like it would on its own studio album.

    Tracks whose artist has no stored catalogue are not marked: their fresh
    scores were re-mapped against the compilation's own distribution, which
    is not comparable to any artist scale.  Same-scan album-mates (other
    tracks on this compilation by the same primary artist) join the stored
    catalogue so freshly-scanned tracks are ranked too; the track's own score
    is excluded (it is not yet stored, so including it would double-count).
    """
    try:
        _sd = get_config().get("single_detection") or {}
        _large_catalog_pct = float(_sd.get("artist_top_percentile_large", 0.25) or 0.25)
        _large_catalog_threshold = int(_sd.get("artist_catalog_large_threshold", 30) or 30)
    except Exception:
        _large_catalog_pct, _large_catalog_threshold = 0.25, 30
    # Same-scan scores grouped by PRIMARY track artist (collab strings like
    # "Feuerschwanz feat. Doro" resolve to the same group as "Feuerschwanz").
    by_primary: dict[str, list[float]] = {}
    for _tr in album_results:
        _score = float(_tr.get("popularity_score") or 0)
        if _score <= 0:
            continue
        _primary = strip_featured_artist(str(_tr.get("artist") or ""))
        by_primary.setdefault(_primary, []).append(_score)

    catalogue_cache: dict[str, list[float]] = {}
    for _tr in album_results:
        if bool(_tr.get("exclude_from_stats")):
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
            # No catalogue for this artist (and no same-scan mates) — never
            # fabricate a top-% verdict from the compilation's own mix.
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
    """Stored popularity scores for a track artist's own catalogue.

    The reference is the artist's OWN albums (``album_artist == artist``),
    never the compilation track itself (which is stored under the
    compilation's album artist).  Used to re-map a compilation track's raw
    popularity relative to its track artist instead of the compilation album.

    Collaboration strings are reduced to the PRIMARY artist first, so a
    compilation track credited as "Feuerschwanz feat. Doro" resolves
    Feuerschwanz's own catalogue instead of failing the lookup and falling
    back to the compilation's distribution.  Stored ``final_score`` values
    mix raw + album-relative scales, so each stored album is re-anchored onto
    the album-relative scale (``reanchor_scores_to_album_relative``) — the
    same correction the album path applies — keeping a compilation track's
    fresh relative score comparable to the artist's stored catalogue.
    """
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
            # Album-relative re-anchoring needs the album grouping; bonus /
            # alternate / live titles (whose stored scores sit on the raw
            # scale or are padded) are excluded the same way the album path
            # excludes them.
            db_rows = [
                (str(r[1] or ""), float(r[2] or 0))
                for r in rows or []
                if r[2] and not is_bonus_track_title(str(r[0] or ""))
            ]
    except Exception as exc:
        logger.debug("[scan_runner] Track artist score load failed for %s: %s", track_artist, exc)
        return []
    if not db_rows:
        return []
    try:
        from services.popularity.popularity_math import reanchor_scores_to_album_relative
        return list(reanchor_scores_to_album_relative(db_rows))
    except Exception as exc:
        logger.debug("[scan_runner] Track artist score re-anchor failed for %s: %s", track_artist, exc)
        return [float(s) for _alb, s in db_rows]


def _album_reference_scores(
    album_results: list[dict[str, Any]],
    score_key: str = "popularity_score",
) -> list[float]:
    """Album distribution used as the popularity/scoring reference.

    Bonus / alternate / live tracks (``exclude_from_stats``) on a STUDIO album
    must not pull the album's average scoring down — an album padded with extra
    live cuts would otherwise crush its own track scores against an inflated
    median.  Tracks flagged ``exclude_from_stats`` are therefore dropped from
    the reference distribution, so popularity, z-scores and star bands measure
    the album's core tracks only.

    A true LIVE album flags EVERY track excluded (``album_context_live``), so
    the drop must not empty the reference — when fewer than
    ``ALBUM_RELATIVE_MIN_ALBUM_TRACKS`` eligible tracks remain, the full set is
    used (a live album is scored against itself, as before).
    """
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
    """Re-map freshly-scored album tracks onto the relative 0-100 scale.

    Regular albums: album-relative only (spec rule 1) — the album's freshly
    computed RAW combined scores are re-mapped via
    ``apply_album_relative_popularity`` (album median + scaled-MAD, robust
    z → 0-100) so scores spread within the album and never clump on the
    ceiling.  Artist-wide stats are ignored.  Bonus / alternate / live tracks
    (``exclude_from_stats``) on a studio album are excluded from the reference
    distribution (``_album_reference_scores``) so they do not drag the album's
    average scoring down.

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
    """Re-map compilation tracks onto their track artist's 0-100 scale.

    Each compilation track is re-mapped relative to ITS OWN track artist's
    stored catalogue popularity (median + scaled-MAD via
    ``apply_track_artist_relative_popularity``) — not the compilation album's
    distribution.  Collaboration strings are reduced to the PRIMARY artist
    first ("Feuerschwanz feat. Doro" → "Feuerschwanz"), so the catalogue
    lookup hits the main artist's discography instead of failing on the
    literal collab string.

    Artists without enough stored catalogue scores (``<
    ALBUM_RELATIVE_MIN_ALBUM_TRACKS`` valid values) fall back to the
    compilation's OWN internal raw-score distribution
    (``apply_album_relative_popularity`` against the album's raw scores) —
    the track is still normalized off a raw 85-95 peak instead of sitting on
    it.  Returns the number of tracks re-mapped.
    """
    changed = 0
    rows: list[dict[str, Any]] = []
    artist_scores_cache: dict[str, list[float]] = {}
    # Compilation-internal raw distribution — the fallback reference when a
    # track artist has no usable stored catalogue.
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
            # No usable catalogue — normalize against the compilation's own
            # distribution so the track still lands on the relative scale.
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

    # ── Standardised scan banner ─────────────────────────────────────────
    # Targeted artist/album scans get the same ASCII phase structure as the
    # album import pipeline (Step 1/3 ...), instead of jumping straight into
    # per-track output.
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

    # Full-library scans log the distinct letter groups (#-9, A, B, ...) that
    # WILL be covered, so operators can see the run is wired to advance
    # through the whole alphabet instead of appearing to stop at the first
    # letter section (the per-letter headers below confirm each transition).
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

        Stored ``final_score`` values are a MIX of scales — albums scanned
        before the album-relative re-map keep their raw combined score (hits at
        85-95) while freshly-scanned albums persist values centred at ~50 — so
        each stored album is re-anchored onto the album-relative scale before
        the scores are merged.  Otherwise a handful of raw-scale outliers occupy
        the top of the merged distribution, inflate the top-10% marking cutoff,
        and push genuinely top-10% album-relative tracks below it (and skew
        artist z-scores the same way).
        """
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
                    {"artist": artist},
                ).fetchall()
                db_rows = [
                    (str(r[1] or ""), float(r[2] or 0))
                    for r in rows or []
                    if r[2]
                    and str(r[0] or "").strip().lower() not in scanned_titles
                    and not is_bonus_track_title(str(r[0] or ""))
                ]
        except Exception as exc:
            logger.debug("[scan_runner] Artist DB score fetch failed for %s: %s", artist, exc)
        try:
            from services.popularity.popularity_math import reanchor_scores_to_album_relative
            db_scores = reanchor_scores_to_album_relative(db_rows)
        except Exception as exc:
            logger.debug("[scan_runner] Artist DB score re-anchor failed for %s: %s", artist, exc)
            db_scores = [float(s) for _alb, s in db_rows]
        return db_scores

    def _post_album_stars(
        artist: str,
        album_results: list[dict[str, Any]],
        is_compilation: bool = False,
        is_va_compilation: bool = False,
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
        # Regular albums — INCLUDING single-artist compilations (Greatest
        # Hits) — re-map against the ALBUM's distribution.  Only TRUE
        # Various-Artists compilations re-map each track against its OWN
        # track artist's catalogue instead (the compilation's distribution —
        # the "album artist" reference — is meaningless when every track has
        # a different artist).  Singles-only passes carry no fresh raw scores
        # and are skipped (self-guarded by the helper).
        try:
            _apply_album_relative_normalization(
                album_results,
                is_compilation=is_va_compilation,
            )
        except Exception as exc:
            logger.debug("[scan_runner] Album-relative normalization failed: %s", exc)

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

        # ── Artist top-% popularity marking + medium→high bump ────────────
        # Spec steps 3-4: mark the top of the artist's catalogue by popularity
        # (``popularity_marked``), then upgrade any MEDIUM-confidence single in
        # that range to HIGH.  Two bands:
        #   - top 10%: marked regardless of single status — the marking alone
        #     earns 5★ (spec rule 2: "popular and 5★ without a single source").
        #   - top 20% (widened): marked ONLY when a MEDIUM-confidence single
        #     detection source exists — the bump then promotes it to HIGH → 5★
        #     (spec rule 3).  The widening never marks popularity alone.
        # Both are persisted before star ratings run so the 5★ bump (step 5)
        # sees the upgraded confidence.
        try:
            _top_pct = float(
                (get_config().get("single_detection") or {}).get("artist_top_percentile", 0.10) or 0.10
            )
            _medium_pct = float(
                (get_config().get("single_detection") or {}).get("artist_medium_bump_percentile", 0.20) or 0.20
            )
            _large_pct = float(
                (get_config().get("single_detection") or {}).get("artist_top_percentile_large", 0.25) or 0.25
            )
            _large_threshold = int(
                (get_config().get("single_detection") or {}).get("artist_catalog_large_threshold", 30) or 30
            )
        except Exception:
            _top_pct, _medium_pct, _large_pct, _large_threshold = 0.10, 0.20, 0.25, 30
        if is_va_compilation:
            # TRUE VA compilations rank each track against ITS OWN track
            # artist's catalogue — the album artist ("Various Artists") has no
            # real catalogue, so the album-level cutoffs would rank a track
            # against every other compilation track in the library instead.
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
                    # Bonus / alternate / live tracks (``exclude_from_stats``) are
                    # not part of the artist's core catalogue popularity — they
                    # never consume a top-% slot and never earn 5★ from the
                    # marking (a padded live cut must not outrank a real single).
                    if bool(_tr.get("exclude_from_stats")):
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
            logger.debug("[scan_runner] Per-album star posting failed for %s: %s", artist, exc)
        return False

    for album_index, album_row in enumerate(albums, start=1):

        # ✅ Graceful stop support
        if effective_stop_file and is_stop_requested(effective_stop_file):
            log_unified("Scan stopped by user request")
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

        # Per-album processing line (numbered queue for targeted scans).
        log_unified(
            f"[{album_index}/{total_albums}] Processing: \"{str(album or '').strip()}\" "
            f"({len(tracks or [])} Tracks)"
        )

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
                    log_unified(f"Popularity Scan - Skipping album \"{str(album or '').strip()}\" (scanned within last {skip_days} days)")
                elif tracks:
                    all_scored = all(float(t.get("final_score") or 0) > 0 for t in tracks)
                    all_assessed = all(t.get("single_detection_last_updated") for t in tracks)
                    if all_scored and all_assessed:
                        skip_album = True
                        log_unified(f"Popularity Scan - Skipping album \"{str(album or '').strip()}\" (no changes detected)")
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
                            is_va_compilation=bool(_album_context.get("is_va_compilation")),
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
            # A malformed album (bad track shape / degenerate classification)
            # must never kill the whole scan thread silently — that leaves the
            # progress state stuck as "running" and the scan "fails to resume".
            logger.warning("[scan_runner] Album prep failed for %s - %s: %s", artist, album, exc)
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

        # Determine actual scan type from options for history display
        try:
            record_scan(scan_type, "started", message=f"{scan_type} scan: {artist} - {album}", artist=artist, album=album)

            album_result = enrich_album(
                album_row=album_row,
                album_context=album_context,
                stat_eligible_tracks=stat_eligible_tracks,
                options=options,
            )

            # Live-album detection uses the album TYPE as the authoritative
            # signal once enrichment matches one; refresh the in-memory album
            # context so this scan's singles pass sees it.
            _refresh_album_live_context(
                album,
                album_context,
                track_contexts,
                str((album_result or {}).get("detected_album_type") or ""),
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
                    # Junk-level LB counts (below the 25-listen credibility floor)
                    # are suspect the same way zero counts are: a wrong/split
                    # recording MBID — or a translated title like the Korean
                    # "삐처리" vs the library's English "BLEEP" — leaves a track
                    # at ~0 listens even though the album page shows real counts.
                    # Include those tracks so the album-tracklist fallback (which
                    # matches by position + length too) can correct them.
                    _missing_lb_tracks = [
                        t for t in track_dicts
                        if t.get("title")
                        and int(
                            (prefetched_popularity.get(normalize_for_aggregation(t["title"])) or {}).get("listenbrainz_listens") or 0
                        ) < 25
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
                                log_unified(
                                    f"[scan_runner] Album-tracklist LB match for '{_t.get('title')}' ({artist} - {album}): {_cur['listenbrainz_listens']} listens",
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

            # ── MusicBrainz album-level batch pre-resolution ────────────────
            # The per-track MB metadata lookup is the dominant sequential API
            # cost (a search + recording lookup per track).  Resolve every
            # fresh track of this album in ONE batched Lucene search (chunked
            # server-side by ``lookup_album_metadata``), then track_stage
            # short-circuits its per-track lookup on the results.  Rate limit
            # is unchanged — roughly one request instead of N per-track
            # searches.  Tracks that already carry a recording MBID skip the
            # batch (their metadata is resolved).
            if not _singles_pass and not options.get("popularity_only"):
                try:
                    from services.enrichment.musicbrainz_service import (
                        MusicBrainzService,
                        get_shared_mb_client,
                    )
                    _mb_entries: list[tuple[str, str]] = []
                    for _tc in track_contexts:
                        if _tc.get("recording_mbid") or _tc.get("mbid") or _tc.get("musicbrainz_trackid"):
                            continue
                        _tt = _tc.get("title")
                        _aa = _tc.get("artist")
                        if _tt and _aa:
                            _mb_entries.append((str(_tt), str(_aa)))
                    if _mb_entries:
                        _mb_batch = MusicBrainzService(
                            http_client=get_shared_mb_client()
                        ).lookup_album_metadata(_mb_entries)
                        if _mb_batch:
                            # Per-track MB releases for multi-edition albums
                            # ("OPVS NOIR Vol. 3" vs "... (Instrumental)") can
                            # resolve to different releases per track, which
                            # would split this folder into several albums on
                            # every metadata scan.  Collapse the whole batch
                            # onto ONE folder-anchored album name first.
                            _collapse_album_mb_batch(_mb_batch, track_contexts, album)
                            _existing = options.get("mb_batch_metadata") or {}
                            options["mb_batch_metadata"] = {**_existing, **_mb_batch}
                            log_unified(
                                f"[POPULARITY] MusicBrainz batch resolved {len(_mb_batch)}/{len(_mb_entries)} track(s) for {artist} - {album}"
                            )
                except Exception as exc:
                    logger.debug(
                        "[scan_runner] MusicBrainz album batch failed for %s - %s: %s",
                        artist, album, exc,
                    )

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
                        # Frozen tracks reuse the stored score — still log the
                        # result so the unified log shows every track's rating.
                        # Metadata passes compute no scores: their all-zero
                        # lines read like failures, so they log at DEBUG only.
                        if not options.get("metadata_only") and isinstance(frozen_result, dict):
                            _ft = prepared_track.get("title", "Unknown Track")
                            _fs = frozen_result.get("popularity_score")
                            log_unified(
                                f"[TRACK_RESULT] '{_ft}' -> Final: {float(_fs or 0.0):.1f} (frozen | LF: {float(frozen_result.get('lastfm_score') or 0.0):.1f} | LB: {float(frozen_result.get('listenbrainz_score') or 0.0):.1f})",
                            )
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
                    # exactly how each track was scored (LF / LB / final).
                    # Metadata passes compute no scores — their all-zero lines
                    # read like failures, so they log at DEBUG only (the
                    # track_stage [TRACK] line already does the same).
                    if not options.get("metadata_only") and isinstance(track_result, dict):
                        title = prepared_track.get("title", "Unknown Track")
                        f_score = track_result.get("popularity_score")
                        lf = track_result.get("lastfm_score")
                        lb = track_result.get("listenbrainz_score")
                        log_unified(
                            f"[TRACK_RESULT] '{title}' -> Final: {float(f_score or 0.0):.1f} (LF: {float(lf or 0.0):.1f} | LB: {float(lb or 0.0):.1f})",
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
                    is_va_compilation=bool(album_context.get("is_va_compilation")),
                )

        except Exception as _album_exc:
            # A failure in ANY part of one album's processing (enrichment, the
            # prefetch, a single track, cover detection, star posting) must never
            # kill the whole scan worker thread — that leaves the progress state
            # stuck as "running" and the scan "fails to resume".  Log, record a
            # failure, and move on to the next album (legacy behaviour).
            logger.warning("[scan_runner] Album failed for '%s - %s': %s", artist, album, _album_exc)
            try:
                log_unified(f"[POPULARITY] Album '{artist} - {album}' failed ({_album_exc})")
            except Exception:
                pass
            try:
                record_scan(scan_type, "failed", message=f"Album failed: {_album_exc}", artist=artist, album=album)
            except Exception:
                pass
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
