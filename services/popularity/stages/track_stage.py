"""
Per-track popularity/enrichment stage.

This is the ONLY place that connects:
- enrichment external APIs
- popularity scoring
- single detection
- persistence

Uses the updated ``api_clients`` classes directly for lookups.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# API clients (updated versions)
from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import ListenBrainzClient

# Enrichment services (better metadata than raw API clients)
from services.enrichment.musicbrainz_service import (
    MusicBrainzService,
    get_shared_mb_client,
)

# Popularity
from services.popularity.popularity_math import (
    calculate_combined_popularity_score,
    calculate_listenbrainz_percentile,
    evaluate_listenbrainz_validity,
    fmt_count as _fmt_count,
)
from services.popularity.popularity_config import (
    get_live_weight_penalty,
    get_metadata_score_floor,
    get_single_boost,
    resolve_weights,
)

# Provider aggregation helpers (split-variant merging, cross-release lookups)
from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
    get_aggregated_listenbrainz_popularity,
    get_search_aggregated_lastfm_popularity,
)

# Detection
from services.enrichment.single_detection_service import (
    detect_single_for_track,
)

# Cover detection
from services.enrichment.cover_detection_service import (
    detect_cover_song,
)

# Track classification (bonus/live/alternate title detection)
from services.catalog.album_classification_service import (
    is_live_or_alternate_track_title,
)

# Genre aggregation
from services.enrichment.genre_aggregation_service import (
    aggregate_genres,
)

# DB
from db.repositories.tracks import (
    insert_or_update_track,
)
from helpers.normalization_service import safe_int, safe_str

# Re-fetch threshold provider — returns hours based on track release age.
from services.popularity.popularity_cache_policy import (
    get_cache_duration_hours,
    should_use_cached_score,
)


# ── Consolidated per-track log helpers ────────────────────────────────────

_SOURCE_LABELS = {
    "discogs": "Discogs",
    "musicbrainz": "MB",
    "musicbrainz_compilation": "MB-Comp",
    "discogs_video": "Video",
    "lastfm": "LF",
    "radio_edit": "Radio",
}


def _single_chips(sources_raw) -> str:
    """Render the matched/unmatched single-detection sources as chips."""
    try:
        raw = sources_raw or ""
        if isinstance(raw, str):
            sources = json.loads(raw) if raw.strip() else []
        else:
            sources = raw
    except Exception:
        return ""
    chips: list[str] = []
    for s in sources if isinstance(sources, list) else []:
        if not isinstance(s, dict):
            continue
        src = str(s.get("source") or "")
        label = _SOURCE_LABELS.get(src, src)
        chips.append(f"{label}: {'✓' if bool(s.get('matched')) else '✖'}")
    return "[" + ", ".join(chips) + "]" if chips else ""

# Score adjustments (album-relative only — artist-wide stats are ignored by
# design so popularity measures strength within the album, not the catalogue).

logger = logging.getLogger(__name__)


_as_str = safe_str
_as_int = safe_int

# Per-artist Spotify genre cache (artist name → list of genres).  Artist-level
# Spotify genres rarely change, so a full scan only pays the artist lookup once
# per artist instead of once per track.
_spotify_artist_genres_cache: dict[str, list[str]] = {}


def _build_effective_track(
    track: dict[str, Any],
    update_payload: dict[str, Any],
) -> dict[str, Any]:
    effective_track = dict(track)
    effective_track.update(update_payload)
    return effective_track


# Album-type columns are owned by the album stage (``enrich_album`` /
# ``ensure_album_type``).  The per-track stage never computes them, so it
# must not write them back: the in-memory ``track`` dict is loaded before
# album enrichment runs, and re-saving its stale album-type value would
# clobber the freshly-detected type (album page shows "Unknown").
_ALBUM_TYPE_COLUMNS = frozenset({"musicbrainz_albumtype", "spotify_album_type", "releasetype"})
# Album-level MusicBrainz identifiers — owned by the album stage's enrichment
# pass, which resolves the release-group MBID to a concrete release MBID and
# persists musicbrainz_album_mbid / musicbrainz_albumid / musicbrainz_releasegroupid
# for tracks missing them. The track contexts are prepared BEFORE that pass
# runs, so the loaded in-memory values are stale and would otherwise clobber
# the fresh album-stage writes on every upsert.
_ALBUM_MBID_COLUMNS = frozenset({
    "musicbrainz_album_mbid", "musicbrainz_albumid", "musicbrainz_releasegroupid",
})
# Columns the track stage must NEVER write back from stale in-memory values:
# album-type columns (the album stage owns those) and ``title`` — the album
# stage renames covers to "Title (Artist Cover)" and live/acoustic tracks
# AFTER the track contexts were prepared, so the loaded title here is stale.
_STALE_PROTECTED_COLUMNS = frozenset({"title"}) | _ALBUM_TYPE_COLUMNS | _ALBUM_MBID_COLUMNS


def _strip_album_type_columns(
    track: dict[str, Any],
    update_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the effective track dict to persist.

    All of ``update_payload`` (fresh scores, listener counts, MBIDs, single
    detection, ...) is applied over the loaded ``track``, then any column the
    track stage did NOT update is dropped so the stale in-memory value
    (loaded before album enrichment ran) never clobbers what the album stage
    just persisted (album type, cover/live title renames).
    """
    result = dict(track)
    result.update(update_payload)
    for col in _STALE_PROTECTED_COLUMNS:
        if col not in update_payload:
            result.pop(col, None)
    return result


def process_track(
    *,
    track: dict[str, Any],
    track_context: dict[str, Any],
    album_context: dict[str, Any],
    album_result: dict[str, Any],
    options: dict[str, Any],
    album_lb_listens: list[int] | None = None,
    artist_max_lf_listeners: int = 0,
    artist_lf_context: dict[str, Any] | None = None,
    mb_cached_singles: set | None = None,
    discogs_cached_singles: set | None = None,
    discogs_cached_promos: set | None = None,
    prefetched_popularity: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:

    raw_track_id = track.get("id")
    if not raw_track_id:
        return None

    track_id = _as_str(raw_track_id)
    track_title = _as_str(track.get("title"))
    track_artist = _as_str(track.get("artist"))
    from helpers.logging_config import log_unified

    # Per-track progress is emitted ONCE, as a single consolidated line at
    # the end of processing (score + ISRC + single verdict) — the unified log
    # stays one line per track instead of four.
    logger.debug("[TRACK_STAGE] Processing track: %s - %s (%s)", track_artist, track_title, track_id)

    metadata_only = bool(options.get("metadata_only"))
    popularity_only = bool(options.get("popularity_only"))
    frozen_track = bool(options.get("frozen_track"))
    # Singles-only pass: used when an album is skipped for popularity (already
    # scored / recently scanned) but singles detection must still run so the
    # per-album singles output appears (legacy parity).  The metadata,
    # popularity, cover and genre sections are skipped; only singles detection
    # runs, and the stored popularity is carried through unchanged.
    singles_detection_only = bool(options.get("singles_detection_only"))
    # Standalone singles scan (singles_only / singles_with_missing_popularity):
    # singles detection is the whole point — popularity is only fetched for
    # tracks with NO stored popularity data, because singles detection's
    # z-score / top-50% gates need SOME score signal.
    singles_pass = bool(options.get("singles_only")) or bool(options.get("singles_with_missing_popularity"))
    _has_stored_popularity = (
        float(track.get("final_score") or track.get("popularity") or 0) > 0
        or int(track.get("lastfm_listeners") or 0) >= 25
        or int(track.get("listenbrainz_listens") or 0) >= 25
    )

    update_payload: dict[str, Any] = {}
    score_data: dict[str, Any] = {}
    lb_percentile: float = 0.0
    lastfm_listeners: int = 0
    listenbrainz_listens: int = 0
    # Consolidated per-track log parts (emitted as ONE line before returning).
    _isrc_found: str = ""
    _pop_summary: str = ""
    _single_summary: str = ""

    if singles_detection_only or (singles_pass and _has_stored_popularity):
        # Carry the stored popularity through so the result dict and star
        # rating pass see the album's existing scores instead of 0, and so
        # singles detection still gets a popularity signal to work with.
        score_data = {
            "combined_score": float(
                track.get("final_score")
                or track.get("popularity")
                or track.get("popularity_score")
                or 0
            ),
            "lastfm_score": float(track.get("lastfm_score") or 0),
            "listenbrainz_score": float(track.get("listenbrainz_score") or 0),
            "age_score": float(track.get("age_score") or 0),
        }
        lastfm_listeners = _as_int(track.get("lastfm_listeners") or 0)
        listenbrainz_listens = _as_int(track.get("listenbrainz_listens") or 0)
        lb_percentile = float(track.get("lb_percentile") or 0)

    # -------------------------------------------------------------------------
    # 1. POPULARITY (via updated api_clients)
    # -------------------------------------------------------------------------

    if (
        not metadata_only
        and not singles_detection_only
        and not (singles_pass and _has_stored_popularity)
    ):
        try:
            effective_track = _build_effective_track(track, update_payload)

            artist = _as_str(
                track_context.get("artist") or effective_track.get("artist")
            )
            # raw_title keeps any "(feat. Guest)" marker intact — the cleaned
            # lastfm_title below strips brackets, which would otherwise hide
            # featured tracks from the feat detection / search correlation.
            raw_title = _as_str(effective_track.get("title") or track.get("title"))
            title = _as_str(
                track_context.get("lastfm_title") or raw_title
            )
            release_date = _as_str(
                effective_track.get("year") or effective_track.get("release_year")
            )
            recording_mbid = (
                effective_track.get("recording_mbid")
                or effective_track.get("mbid")
                or effective_track.get("musicbrainz_trackid")
            )
            # ISRC pool — the most precise cross-source key a track can carry
            # (unique per recording).  Used for the ISRC fallback arms of the
            # Last.fm / ListenBrainz lookups and persisted from MusicBrainz
            # batch resolution when the file tags don't carry one.
            isrc = _as_str(effective_track.get("isrc") or "").strip()
            if not isrc:
                # The album-level MusicBrainz batch (run once per album by the
                # scan runner) resolves each fresh track to its recording —
                # including the ISRC — in one batched search.  Adopt it HERE,
                # before the popularity fetch and singles detection run, so the
                # ISRC fallback arms (Last.fm / ListenBrainz by-recording) and
                # the ISRC single check can use it on the FIRST scan (the
                # metadata step previously only persisted it for later scans).
                _batch_mb = options.get("mb_batch_metadata") or {}
                _mb_entry = _batch_mb.get(
                    f"{artist.lower()}::{str(raw_title or title).lower()}"
                )
                _batch_isrc = _as_str((_mb_entry or {}).get("isrc") or "").strip()
                if _batch_isrc:
                    isrc = _batch_isrc
                    update_payload["isrc"] = _batch_isrc
            if isrc:
                _isrc_found = isrc
                logger.debug("[TRACK_STAGE] [ISRC_POOL] Found ISRC: %s", isrc)

            # ── Staleness check ───────────────────────────────────────────
            # Skip API calls if fresh-enough data is already in the DB.
            # Cache duration varies by track age: older tracks change less.
            from datetime import datetime, timezone

            def _as_utc(value):
                # DB TIMESTAMP columns return NAIVE datetimes; ``now_ts`` is
                # UTC-aware.  Coerce stored values to aware-UTC so the
                # subtraction never mixes offset-naive and offset-aware.
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        return value.replace(tzinfo=timezone.utc)
                    return value.astimezone(timezone.utc)
                return None

            now_ts = datetime.now(timezone.utc)
            _track_year = effective_track.get("year") or effective_track.get("release_year")
            _cache_ttl = get_cache_duration_hours(_track_year)
            last_lf_ts = _as_utc(effective_track.get("lastfm_last_updated"))
            last_mb_ts = _as_utc(effective_track.get("musicbrainz_last_updated"))
            has_fresh_lf = (
                last_lf_ts is not None
                and (now_ts - last_lf_ts).total_seconds() < _cache_ttl * 3600
            )
            has_fresh_mb = (
                last_mb_ts is not None
                and (now_ts - last_mb_ts).total_seconds() < _cache_ttl * 3600
            )

            # ── Overall cache gate ────────────────────────────────────────
            # If the track has a fresh Spotify-style cached score AND already
            # has a valid final_score, skip all API re-fetches entirely.
            # Frozen mature tracks are ALWAYS routed through the cached path
            # so the popularity score is reused without API calls while the
            # rest of the pipeline (singles/cover/genre) still runs.
            # Forced scans bypass the cache (legacy ``if not (FORCE_RESCAN or force)``).
            # Cached scores with BOTH provider counts at zero are treated as
            # suspect (a failed fetch from a broken scan, missing key, or
            # outage) and re-fetched so scans self-heal instead of caching 0s.
            # Cached scores with only junk-level provider counts (both sources
            # below 25) are treated as suspect — a wrong-artist Last.fm match
            # or a failed fetch from an earlier scan — and re-fetched so scans
            # self-heal instead of caching garbage for the whole TTL.
            _force = bool(options.get("force"))
            _has_credible_data = (
                int(effective_track.get("lastfm_listeners") or 0) >= 25
                or int(effective_track.get("listenbrainz_listens") or 0) >= 25
            )
            # Forced scans ALWAYS recheck: bypass the cached score even for
            # frozen mature tracks (legacy ``FORCE_RESCAN`` behaviour).
            _cached = (
                not _force
                and (frozen_track or should_use_cached_score(effective_track))
            ) and bool(
                effective_track.get("final_score") and _has_credible_data
            )
            if _cached:
                logger.debug(
                    "[track_stage] Using cached score for %s (final_score=%.1f)",
                    track_id,
                    effective_track["final_score"],
                )
                lastfm_listeners = _as_int(effective_track.get("lastfm_listeners") or 0)
                lastfm_playcount = _as_int(effective_track.get("lastfm_playcount") or 0)
                listenbrainz_listens = _as_int(effective_track.get("listenbrainz_listens") or 0)
                listenbrainz_users = _as_int(effective_track.get("listenbrainz_users") or 0)
                # Cached scores reuse the stored LB count as-is — the realism
                # check only runs on freshly-fetched values.
                _score_lb = listenbrainz_listens
                score_data = {
                    "combined_score": float(effective_track.get("final_score", 0)),
                    "lastfm_score": float(effective_track.get("lastfm_score", 0)),
                    "listenbrainz_score": float(effective_track.get("listenbrainz_score", 0)),
                    "age_score": float(effective_track.get("age_score", 0)),
                }
                update_payload["_cached"] = True
            else:
                # --- Last.fm ---
                lastfm_listeners = _as_int(effective_track.get("lastfm_listeners") or 0)
                lastfm_playcount = _as_int(effective_track.get("lastfm_playcount") or 0)
                # Bulk-cache fast-path: when the scan prefetched artist-wide
                # popularity into track_popularity_cache, use it instead of a
                # per-track API call.  Forced scans bypass the cache, EXCEPT
                # for entries freshly resolved from THIS album's tracklist
                # during this scan — those are authoritative.
                # Keys are NORMALISED titles so a feat. variant of the same
                # song ("Herzblut (feat. X)" cached as "herzblut") is found.
                _prefetch_entry = (prefetched_popularity or {}).get(
                    normalize_for_aggregation(title or "")
                )
                if _force and _prefetch_entry and not _prefetch_entry.get("_album_tracklist"):
                    _prefetch_entry = None
                # Fresh-but-suspect values are re-fetched so scans self-heal:
                # zero counts (failed fetch / missing key), or both sources
                # below 25 (wrong-artist match cached by an earlier scan).
                # Forced scans always re-fetch regardless of freshness.
                if (
                    _force
                    or not has_fresh_lf
                    or lastfm_listeners == 0
                    or (lastfm_listeners < 25 and listenbrainz_listens < 25)
                ):
                    if _prefetch_entry and _prefetch_entry.get("lastfm_listeners"):
                        lastfm_listeners = _as_int(_prefetch_entry.get("lastfm_listeners") or 0)
                        lastfm_playcount = _as_int(_prefetch_entry.get("lastfm_playcount") or 0)
                        update_payload["lastfm_listeners"] = lastfm_listeners
                        update_payload["lastfm_playcount"] = lastfm_playcount
                        update_payload["lastfm_last_updated"] = now_ts
                        update_payload["_from_prefetch"] = True
                        # The bulk cache prefetch carries each track's Last.fm
                        # top-tags (captured from the single artist.getTopTracks
                        # call) — persist them when the track has none. Without
                        # this, the prefetch fast-path (which skips per-track
                        # track.getInfo calls) never collected lastfm_tags, so
                        # Last.fm-only genre data never reached the DB.
                        if not effective_track.get("lastfm_tags") and _prefetch_entry.get("lastfm_tags"):
                            update_payload["lastfm_tags"] = _prefetch_entry["lastfm_tags"]
                    else:
                        try:
                            from helpers.config_helpers import get_config
                            _lf_cfg = get_config().get("api_integrations", {}).get("lastfm", {})
                            _lf_api_key = _lf_cfg.get("api_key", "")
                            if _lf_api_key:
                                lf = LastFmClient(_lf_api_key)
                                # Prefer the aggregated fetch which merges split
                                # Last.fm variants ("Song" vs "Song (Radio Edit)")
                                # and falls back to a single track.getInfo lookup.
                                # ``isrc`` + ``recording_mbid`` feed the ISRC
                                # fallback arm when the primary lookup is empty.
                                agg = get_aggregated_lastfm_popularity(
                                    artist,
                                    raw_title or title,
                                    lastfm_client=lf,
                                    isrc=isrc or None,
                                    recording_mbid=recording_mbid or None,
                                )
                                if agg and (agg.get("listeners") or 0) > 0:
                                    lastfm_listeners = _as_int(agg.get("listeners") or 0)
                                    lastfm_playcount = _as_int(agg.get("track_play") or agg.get("playcount") or 0)
                                    lf_result = {}
                                    _variant_detail = agg.get("variant_detail") or {}
                                    if _variant_detail:
                                        log_unified(
                                            f"[TRACK_STAGE] [POPULARITY] Queried {agg.get('sources_queried')} variants -> "
                                            f"Max listeners: {lastfm_listeners:,} "
                                            f"({' | '.join(f'{k}: {v:,}' for k, v in _variant_detail.items())})"
                                        )
                                else:
                                    lf_result = lf.get_track_info(artist, title)
                                    lastfm_listeners = _as_int(lf_result.get("listeners") if isinstance(lf_result, dict) else 0)
                                    lastfm_playcount = _as_int(lf_result.get("track_play") if isinstance(lf_result, dict) else 0)
                                update_payload["lastfm_listeners"] = lastfm_listeners
                                update_payload["lastfm_playcount"] = lastfm_playcount
                                update_payload["lastfm_last_updated"] = now_ts
                                toptags = lf_result.get("toptags", {}) if isinstance(lf_result, dict) else {}
                                tag_list = toptags.get("tag", []) if isinstance(toptags, dict) else []
                                if tag_list:
                                    import json
                                    update_payload["lastfm_tags"] = json.dumps(
                                        [t.get("name", "") for t in tag_list if isinstance(t, dict) and t.get("name")]
                                    )
                            else:
                                lastfm_listeners = 0
                                lastfm_playcount = 0
                        except Exception:
                            lastfm_listeners = 0
                            lastfm_playcount = 0

                # ── Featured-artist search correlation ────────────────────
                # The album version of a feat. track (few listens) is what the
                # prefetch / artist top-tracks usually surface, while the
                # single version carries the real popularity.  For feat.
                # tracks search Last.fm for every published version of the
                # song and keep the higher combined count — a separate method
                # of correlating all versions of a song.
                _is_feat_variant = (
                    "feat" in str(artist or "").casefold()
                    or "feat" in str(raw_title or "").casefold()
                    or "feat" in str(title or "").casefold()
                )
                if _is_feat_variant and (
                    bool(update_payload.get("_from_prefetch"))
                    or lastfm_listeners == 0
                ):
                    try:
                        from helpers.config_helpers import get_config as _get_cfg2
                        _lf_key2 = (_get_cfg2().get("api_integrations", {}).get("lastfm", {}) or {}).get("api_key", "") or ""
                        if _lf_key2:
                            _lf2 = LastFmClient(_lf_key2)
                            _search_agg = get_search_aggregated_lastfm_popularity(
                                artist, raw_title or title, lastfm_client=_lf2,
                            ) or {}
                            _search_listeners = _as_int(_search_agg.get("listeners") or 0)
                            if _search_listeners > lastfm_listeners:
                                lastfm_listeners = _search_listeners
                                lastfm_playcount = _as_int(_search_agg.get("track_play") or 0)
                                update_payload["lastfm_listeners"] = lastfm_listeners
                                update_payload["lastfm_playcount"] = lastfm_playcount
                                update_payload["lastfm_last_updated"] = now_ts
                                logger.info(
                                    "[track_stage] Featured-artist Last.fm search boost for %s: %s listeners across %s version(s)",
                                    track_id, lastfm_listeners, len(_search_agg.get("matched_tracks") or []),
                                )
                    except Exception as exc:
                        logger.debug("[track_stage] Last.fm search aggregation failed for %s: %s", track_id, exc)

                # --- ListenBrainz ---
                listenbrainz_listens = _as_int(effective_track.get("listenbrainz_listens") or 0)
                listenbrainz_users = _as_int(effective_track.get("listenbrainz_users") or 0)
                last_lb_ts = _as_utc(effective_track.get("listenbrainz_last_updated"))
                has_fresh_lb = (
                    last_lb_ts is not None
                    and (now_ts - last_lb_ts).total_seconds() < _cache_ttl * 3600
                )
                # Fresh-but-zero is suspect (broken prior scan): re-fetch.
                # Forced scans always re-fetch regardless of freshness.
                if _force or not has_fresh_lb or listenbrainz_listens == 0:
                    _lb_source = "none"
                    # Bulk-cache fast-path first: prefetched artist-wide data
                    # from track_popularity_cache (non-forced scans only).
                    if _prefetch_entry and _prefetch_entry.get("listenbrainz_listens"):
                        _lb_source = "prefetch"
                        listenbrainz_listens = _as_int(_prefetch_entry.get("listenbrainz_listens") or 0)
                        listenbrainz_users = _as_int(_prefetch_entry.get("listenbrainz_users") or 0)
                        # Adopt the album recording MBID when the prefetch
                        # came from the album tracklist — keeps the stored
                        # MBID on the album version so future lookups match
                        # the ListenBrainz album page instead of a random
                        # split recording.
                        _album_rec_mbid = _prefetch_entry.get("recording_mbid")
                        if _album_rec_mbid and _album_rec_mbid != recording_mbid:
                            recording_mbid = _album_rec_mbid
                            update_payload["recording_mbid"] = _album_rec_mbid
                            update_payload["mbid"] = _album_rec_mbid
                    else:
                        # Single-MBID fallback. ListenBrainz popularity is
                        # keyed by recording MBID — resolve one via the cached
                        # MusicBrainz suggestion when the track has none,
                        # otherwise tracks that exist on ListenBrainz read 0
                        # forever. The resolved MBID is persisted so later
                        # scans skip the lookup.
                        # ``raw_title`` is passed (not the cleaned ``title``,
                        # which strips brackets via lastfm_title) so an
                        # alternate version like "Song (Live)" resolves to its
                        # OWN recording instead of matching the studio original.
                        if listenbrainz_listens == 0 and not recording_mbid and (raw_title or title) and artist:
                            try:
                                # ISRC arm FIRST: an ISRC is a unique recording
                                # key, so its MusicBrainz lookup is exact —
                                # far more reliable than the fuzzy title search
                                # (and one fewer request when it hits).
                                if isrc:
                                    from services.popularity.popularity_sources import (
                                        resolve_isrc_recording,
                                    )
                                    _isrc_rec = resolve_isrc_recording(
                                        isrc, title=raw_title or title, artist=artist,
                                    )
                                    if _isrc_rec and _isrc_rec.get("recording_mbid"):
                                        recording_mbid = _isrc_rec["recording_mbid"]
                                        _lb_source = "isrc_resolved"
                                        logger.debug(
                                            "[TRACK_STAGE] [ISRC_POOL] ISRC %s -> recording %s",
                                            isrc, recording_mbid,
                                        )
                                if not recording_mbid:
                                    # Album-level batch pre-resolution first (one
                                    # batched search served the whole album's
                                    # MBIDs), then the per-track cached suggestion.
                                    _batch_mb = options.get("mb_batch_metadata") or {}
                                    _mb_entry = _batch_mb.get(
                                        f"{artist.lower()}::{str(raw_title or title).lower()}"
                                    )
                                    if _mb_entry and _mb_entry.get("recording_mbid"):
                                        recording_mbid = _mb_entry["recording_mbid"]
                                    else:
                                        recording_mbid, _conf = MusicBrainzService(
                                            http_client=get_shared_mb_client()
                                        ).get_suggested_mbid(raw_title or title, artist)
                                if recording_mbid:
                                    _lb_source = _lb_source or "mbid_resolved"
                                    update_payload["recording_mbid"] = recording_mbid
                                    update_payload["mbid"] = recording_mbid
                            except Exception:
                                recording_mbid = None
                        if listenbrainz_listens == 0 and recording_mbid:
                            _lb_source = "single_lookup"
                            try:
                                lb = ListenBrainzClient()
                                lb_result = lb.get_recording_popularity(recording_mbid) if recording_mbid else {}
                                listenbrainz_listens = _as_int(lb_result.get("total_listen_count") if isinstance(lb_result, dict) else 0)
                                listenbrainz_users = _as_int(lb_result.get("total_user_count") if isinstance(lb_result, dict) else 0)
                            except Exception:
                                listenbrainz_listens = 0
                                listenbrainz_users = 0
                    update_payload["listenbrainz_listens"] = listenbrainz_listens
                    update_payload["listenbrainz_users"] = listenbrainz_users
                    update_payload["listenbrainz_last_updated"] = now_ts
                    logger.debug(
                        "[track_stage] LB for %s (%s - %s): %s listens / %s users (source=%s, mbid=%s)",
                        track_id, artist, title, listenbrainz_listens, listenbrainz_users, _lb_source, recording_mbid or "-",
                    )

                # ── Cross-release ListenBrainz aggregation ─────────────────
                # When ListenBrainz came back EMPTY (or Last.fm is weak / a
                # "feat." split exists), search ALL recordings of this track
                # by the same artist and sum their ListenBrainz counts —
                # split recordings (single vs album version) otherwise report
                # 0 or tiny counts for the resolved MBID while the real
                # listens sit on another recording.
                _feat_variant = (
                    "feat" in str(raw_title or title).lower()
                    or "feat" in str(artist or "").lower()
                )
                if (listenbrainz_listens == 0 or lastfm_listeners < 20 or _feat_variant) and title and artist:
                    try:
                        agg_lb = get_aggregated_listenbrainz_popularity(
                            title=title,
                            artist=artist,
                            primary_mbid=recording_mbid,
                            isrc=isrc or None,
                        )
                        agg_total = _as_int((agg_lb or {}).get("total_listen_count") or 0)
                        if agg_total > listenbrainz_listens:
                            listenbrainz_listens = agg_total
                            update_payload["listenbrainz_listens"] = listenbrainz_listens
                            logger.debug("[track_stage] Cross-release LB boost for %s: %s listens", track_id, agg_total)
                    except Exception as exc:
                        logger.debug("[track_stage] Cross-release LB aggregation failed for %s: %s", track_id, exc)

                # A "(Live)"/"(Acoustic)" title on a STUDIO album is still a
                # live recording: flag it so the live weight penalty applies
                # (and the 4★ cap later).  Live albums are already flagged via
                # ``album_context_live`` / ``is_live_album``.
                is_live_flag = bool(
                    effective_track.get("is_live")
                    or effective_track.get("album_context_live")
                    or album_context.get("is_live_album")
                    or is_live_or_alternate_track_title(raw_title or title)
                )
                is_featured_flag = bool(
                    "feat" in str(artist or "").lower()
                    or "feat" in str(raw_title or title).lower()
                )
                has_mb_meta = bool(recording_mbid)
                prior_single = bool(effective_track.get("is_single"))

                # Dynamic Last.fm weight from artist listener context (legacy
                # parity): boosts the Last.fm weight for catalogue outliers and
                # reduces it for underperformers.  The base weight is resolved
                # LIVE from config so popularity.weights edits apply without a
                # process restart.
                lastfm_weight_override = None
                if artist_lf_context and (artist_lf_context.get("total") or 0) > 0 and lastfm_listeners > 0:
                    try:
                        from services.enrichment.single_detection_context_service import get_dynamic_lastfm_weight
                        _live_lf_base, _, _ = resolve_weights()
                        lastfm_weight_override = get_dynamic_lastfm_weight(
                            artist_lf_context,
                            int(lastfm_listeners or 0),
                            _live_lf_base,
                        )
                    except Exception as exc:
                        logger.debug("[track_stage] Dynamic LF weight failed for %s: %s", track_id, exc)

                # Adjustable scoring knobs from config (single_detection section).
                try:
                    cfg_single_boost = get_single_boost()
                    cfg_floor = get_metadata_score_floor()
                    cfg_live_penalty = get_live_weight_penalty()
                except Exception:
                    cfg_single_boost, cfg_floor, cfg_live_penalty = 1.15, 5.0, 0.5

                # Album Last.fm listener distribution (legacy parity): score
                # Last.fm against the ALBUM's own listener spread so the track
                # with the most listeners ranks accordingly even when the
                # artist has a bigger hit elsewhere.  The values come from the
                # fresh artist prefetch, filtered to THIS album's tracks —
                # previously the whole artist catalogue was used, which
                # crushed every album track below the artist's biggest hits.
                # The same filter builds the album's LF/LB ratio pairs used by
                # the ListenBrainz realism check below.
                _album_lf_listeners = None
                _album_lf_lb_pairs: list[tuple[int, int]] = []
                try:
                    _album_titles = {
                        normalize_for_aggregation(str(t.get("title") or ""))
                        for t in (album_context.get("tracks") or [])
                    }
                    # Deluxe/expanded albums pad the tracklist with
                    # live/acoustic/demo/bonus cuts — their low listener
                    # counts must not drag the core tracks' album-local z
                    # down.  Drop every album track flagged for exclusion or
                    # matching the live/alternate title patterns from the
                    # distribution (same rule as the star-rating baseline).
                    # A genuine LIVE album flags everything: fewer than 3 core
                    # tracks then falls back to the full tracklist, so it is
                    # still scored against itself (as before).
                    _excluded_titles = {
                        normalize_for_aggregation(str(t.get("title") or ""))
                        for t in (album_context.get("tracks") or [])
                        if bool(t.get("exclude_from_stats"))
                        or bool(t.get("is_live"))
                        or is_live_or_alternate_track_title(str(t.get("title") or ""))
                    }
                    _all_lf_vals = []
                    _lf_vals = []
                    for _k, _e in (prefetched_popularity or {}).items():
                        _norm_k = normalize_for_aggregation(str(_k or ""))
                        if _norm_k not in _album_titles:
                            continue
                        _lfv = int(_e.get("lastfm_listeners") or 0)
                        _lbv = int(_e.get("listenbrainz_listens") or 0)
                        if _lfv > 0:
                            _all_lf_vals.append(_lfv)
                        if _norm_k not in _excluded_titles:
                            if _lfv > 0:
                                _lf_vals.append(_lfv)
                            if _lfv > 0 and _lbv > 0:
                                _album_lf_lb_pairs.append((_lfv, _lbv))
                    if len(_lf_vals) < 3:
                        _lf_vals = _all_lf_vals
                    if len(_lf_vals) >= 3:
                        _album_lf_listeners = _lf_vals
                except Exception:
                    _album_lf_listeners = None

                # ── ListenBrainz realism check ───────────────────────────
                # A mismatched recording MBID (wrong / split / obscure
                # recording) can surface an unrealistically LOW LB count for a
                # track whose real popularity is healthy, dragging the album
                # average down.  Compare each track's LB against the album's LB
                # distribution (median + 2× MAD) and the album's median LF/LB
                # ratio; when the LB is invalid, score the track on Last.fm
                # alone.  Confirmed singles are never penalised — their LB is
                # legitimate evidence.
                _score_lb = listenbrainz_listens
                try:
                    _lb_valid, _lb_reasons = evaluate_listenbrainz_validity(
                        listenbrainz_listens=listenbrainz_listens,
                        lastfm_listeners=lastfm_listeners,
                        album_lb_listens=album_lb_listens,
                        album_lf_lb_pairs=_album_lf_lb_pairs or None,
                        is_single=bool(prior_single or effective_track.get("is_single")),
                    )
                    if not _lb_valid:
                        _score_lb = 0
                        logger.info(
                            "[track_stage] LB treated as invalid for %s (%s - %s): %s listens (%s) — scoring on Last.fm only",
                            track_id, artist, title, listenbrainz_listens,
                            ", ".join(_lb_reasons),
                        )
                except Exception as exc:
                    logger.debug("[track_stage] LB realism check failed for %s: %s", track_id, exc)

                score_data = calculate_combined_popularity_score(
                    lastfm_listeners=lastfm_listeners,
                    lastfm_artist_max_listeners=artist_max_lf_listeners,
                    listenbrainz_listens=_score_lb,
                    album_lb_listens=album_lb_listens,
                    album_lf_listeners=_album_lf_listeners,
                    age_source_value=_score_lb,
                    release_date=release_date,
                    is_single=prior_single,
                    has_metadata=has_mb_meta,
                    is_featured_track=is_featured_flag,
                    is_live_track=is_live_flag,
                    lastfm_weight_override=lastfm_weight_override,
                    single_boost=cfg_single_boost,
                    metadata_score_floor=cfg_floor,
                    live_weight_penalty=cfg_live_penalty,
                )

            # LB percentile within the album (used by star-rating rescue path).
            # An LB flagged invalid by the realism check is scored as zero so
            # its percentile cannot rescue the track's rating.
            try:
                lb_percentile = calculate_listenbrainz_percentile(_score_lb, album_lb_listens) if album_lb_listens else 0.0
            except Exception:
                lb_percentile = 0.0

            # Apply score_data (whether cached or freshly computed)
            update_payload.update(score_data)

            # Map combined_score → final_score so it persists to the DB.
            combined = score_data.get("combined_score", 0.0)
            update_payload["final_score"] = combined
            update_payload["popularity"] = combined

            # ── Raw score carried for the album-relative re-map ───────────
            # The album-relative normalization (album median + scaled-MAD,
            # robust z → 0-100) is an ALBUM-level operation — it must see ALL
            # of the album's fresh raw scores before re-mapping any of them.
            # It runs as a post-album pass in the scan runner (raw scores are
            # carried here via ``_raw_combined``); artist-wide stats are
            # deliberately ignored so popularity measures strength within the
            # album, never the catalogue.  Only freshly-scored tracks carry a
            # raw score — fully-cached/frozen tracks keep their stored score.
            if not update_payload.get("_cached"):
                update_payload["_raw_combined"] = float(score_data.get("combined_score") or 0)

        except Exception as e:
            # Surface scoring failures at WARNING so a scan that ends with all
            # zero scores (→ 1★ for everything) is diagnosable in the unified log.
            logger.warning("[track_stage][SCORING] %s: %s", track_id, e, exc_info=True)

        # ── Per-track step summary (scanning log) ─────────────────────────
        # One line per track showing whether popularity came from cache or a
        # fresh fetch, the provider counts, and the resulting score.  The
        # parts feed the single consolidated per-track log line.
        try:
            _final_score = float(update_payload.get("final_score") or 0)
            if update_payload.get("_cached"):
                _src = "cached"
            elif update_payload.get("_from_prefetch"):
                _src = "prefetched"
            else:
                _src = "fresh"
            logger.debug(
                "[track_stage] %s - %s popularity %s score=%.1f (LF=%d, LB=%d)",
                track_artist, track_title, _src, _final_score,
                int(lastfm_listeners or 0), int(listenbrainz_listens or 0),
            )
            _pop_summary = (
                f"Score: {_final_score:.1f} "
                f"(LF: {_fmt_count(lastfm_listeners)}, LB: {_fmt_count(listenbrainz_listens)})"
            )
        except Exception:
            _pop_summary = ""

    # -------------------------------------------------------------------------
    # 2. SINGLES DETECTION
    # -------------------------------------------------------------------------

    # Freshness gate: once singles detection has run for a track and the scan
    # isn't forced, reuse the stored result until the cache TTL passes — the
    # per-track Discogs/MusicBrainz searches only run again when the data is
    # stale (or the scan is forced), matching "update only if changed".
    _sd_fresh = False
    if not bool(options.get("force")):
        try:
            from datetime import datetime as _sd_dt, timezone as _sd_tz
            _sd_raw = track.get("single_detection_last_updated")
            if _sd_raw:
                _sd_ts = _sd_raw
                if isinstance(_sd_ts, str):
                    _sd_ts = _sd_dt.fromisoformat(str(_sd_ts).replace("Z", "+00:00"))
                if _sd_ts.tzinfo is None:
                    _sd_ts = _sd_ts.replace(tzinfo=_sd_tz.utc)
                _sd_ttl_hours = get_cache_duration_hours(
                    track.get("year") or track.get("release_year")
                )
                _sd_age_ok = (_sd_dt.now(_sd_tz.utc) - _sd_ts).total_seconds() < _sd_ttl_hours * 3600
                # Only reuse stored results that actually produced evidence.
                # A "low / no matched sources" result usually means the last
                # run hit a transient API failure (rate limit, timeout) —
                # caching it for the full TTL silently disables single
                # detection for that track. No-evidence results are retried
                # on the next scan so detection self-heals.
                _sd_has_evidence = bool(track.get("is_single"))
                if not _sd_has_evidence:
                    try:
                        import json as _sd_json
                        _sd_sources = track.get("single_sources") or ""
                        if isinstance(_sd_sources, str):
                            _sd_sources = _sd_json.loads(_sd_sources) if _sd_sources.strip() else []
                        _sd_has_evidence = any(
                            isinstance(s, dict) and bool(s.get("matched"))
                            for s in (_sd_sources or [])
                        )
                    except Exception:
                        _sd_has_evidence = True  # unparseable — assume valid
                _sd_fresh = _sd_age_ok and _sd_has_evidence
                if _sd_fresh:
                    logger.debug("[track_stage] Singles detection fresh for %s — skipping", track_id)
        except Exception:
            _sd_fresh = False

    if not metadata_only and not popularity_only and not _sd_fresh:
        try:
            from datetime import datetime as _dt, timezone as _tz
            sd_now = _dt.now(_tz.utc)
            effective_track = _build_effective_track(track, update_payload)
            sd_title = _as_str(effective_track.get("title") or "")
            sd_artist = _as_str(effective_track.get("artist") or "")
            sd_album = _as_str(album_context.get("album") or track.get("album") or "")
            sd_album_type = _as_str(album_result.get("detected_album_type") or options.get("album_type") or "")
            # Use the ADJUSTED score (final_score/popularity) so this track's
            # singles-gate signal is on the same scale as the stored album
            # scores it is compared against below (``_album_scores`` reads
            # stored adjusted popularity/final_score).  The raw combined_score
            # would mix raw + adjusted scales in the top-50% comparison.
            sd_popularity = float(
                effective_track.get("final_score")
                or effective_track.get("popularity")
                or effective_track.get("combined_score")
                or effective_track.get("popularity_score")
                or 0
            )

            album_track_count = len(album_context.get("tracks") or []) or 1

            # Resolve optional detection inputs the service can use as
            # corroborating evidence (Discogs token, Last.fm client).
            sd_discogs_token = ""
            try:
                import os as _os
                from helpers.config_helpers import get_config as _get_cfg
                sd_discogs_token = _os.environ.get("DISCOGS_TOKEN", "")
                if not sd_discogs_token:
                    sd_discogs_token = (_get_cfg().get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
                if sd_discogs_token.lower() in ("your_discogs_token", "your_token", "placeholder"):
                    sd_discogs_token = ""
            except Exception:
                sd_discogs_token = ""
            sd_lastfm_client = None
            try:
                from helpers.config_helpers import get_config as _get_cfg
                _lf_key = (_get_cfg().get("api_integrations", {}).get("lastfm", {}) or {}).get("api_key", "") or ""
                if _lf_key:
                    sd_lastfm_client = LastFmClient(_lf_key)
            except Exception:
                sd_lastfm_client = None

            # Top-50% popularity gate: only tracks in the top half of album
            # popularity are checked for singles — except compilation / Various
            # Artists albums, where every track is checked (legacy spec).
            _sd_eligible = True
            if sd_popularity > 0:
                try:
                    # Only TRUE Various-Artists compilations skip the top-50%
                    # gate (every track has a different artist, so ranking
                    # them against each other is meaningless).  Single-artist
                    # compilations (Greatest Hits tagged "compilation") are
                    # treated like standard studio albums: the gate applies.
                    # The album context's authoritative classification is
                    # preferred; the artist/title checks only fill in when the
                    # context is unavailable.
                    _is_comp_album = bool(
                        album_context.get("is_va_compilation")
                        or str(sd_artist or "").strip().lower()
                        in ("various artists", "various", "compilation", "soundtrack")
                        or "various artists" in str(sd_album or "").lower()
                    )
                    if not _is_comp_album:
                        _album_scores = [
                            float(t.get("popularity") or t.get("final_score") or 0)
                            for t in (album_context.get("tracks") or [])
                            if float(t.get("popularity") or t.get("final_score") or 0) > 0
                        ]
                        if len(_album_scores) >= 4:
                            _below = sum(1 for s in _album_scores if s <= sd_popularity)
                            if (_below / len(_album_scores)) < 0.5:
                                _sd_eligible = False
                                logger.debug(
                                    "[track_stage] Singles detection skipped for %s (%s) — below top-50%% album popularity",
                                    track_id, sd_title,
                                )
                except Exception:
                    _sd_eligible = True

            # Manual override (legacy parity): users who explicitly set
            # is_single via the edit form (or the track page "Force (skip
            # auto-scan)" checkbox) must not have auto-detection overwrite
            # their choice — the old scanner filtered these tracks out of
            # detection entirely (WHERE single_manual_override = 0).
            _sd_manual_override = False
            try:
                _sd_manual_override = bool(track.get("single_manual_override"))
            except Exception:
                _sd_manual_override = False

            if _sd_eligible and not _sd_manual_override:
                sd_result = detect_single_for_track(
                    title=sd_title,
                    artist=sd_artist,
                    album_track_count=album_track_count,
                    popularity=sd_popularity,
                    album_type=sd_album_type or None,
                    album=sd_album,
                    # Authoritative VA classification from the album context:
                    # single-artist compilations (Greatest Hits) keep the
                    # normal z-score gates instead of the compilation bypass.
                    is_va_compilation=bool(album_context.get("is_va_compilation")),
                    isrc=effective_track.get("isrc") or None,
                    duration=(
                        float(effective_track["duration"])
                        if effective_track.get("duration")
                        else None
                    ),
                    use_advanced_detection=True,
                    persist_result=False,  # We persist via track_stage
                    mb_cached_singles=mb_cached_singles,
                    discogs_cached_singles=discogs_cached_singles,
                    discogs_cached_promos=discogs_cached_promos,
                    artist_mbid=(
                        effective_track.get("musicbrainz_artistid")
                        or effective_track.get("musicbrainz_artist_id")
                        or effective_track.get("lastfm_artist_mbid")
                    ),
                    listenbrainz_listens=int(listenbrainz_listens or 0),
                    lastfm_listeners=int(lastfm_listeners or 0),
                    discogs_token=sd_discogs_token or None,
                    lastfm_client=sd_lastfm_client,
                    mb_client=get_shared_mb_client(),
                )
            else:
                sd_result = None
                if _sd_manual_override:
                    _single_summary = "Single: SKIPPED (manual override)"
                    logger.debug(
                        "[track_stage] %s - %s → single=skipped (manual override)",
                        track_artist, track_title,
                    )
                else:
                    # Detection was skipped for this track (below the
                    # top-50% album popularity gate) — drop any stale single
                    # flag so the badge and star rating only reflect
                    # confirmed detections. Manual overrides are exempt.
                    # single_detection_last_updated is intentionally kept so
                    # the track still counts as "assessed" for the album
                    # no-changes skip check.
                    update_payload["is_single"] = False
                    update_payload["single_confidence"] = "low"
                    update_payload["single_confidence_score"] = 0.0
                    update_payload["single_sources"] = ""
                    _single_summary = "Single: LOW (below top-50% album popularity)"
                    logger.debug(
                        "[track_stage] %s - %s → single=cleared (below top-50%% album popularity)",
                        track_artist, track_title,
                    )

            if sd_result:
                import json as _json
                update_payload["is_single"] = sd_result.get("is_single", False)
                update_payload["single_confidence"] = sd_result.get("confidence", "low")
                update_payload["single_confidence_score"] = sd_result.get("confidence_score", 0.0)
                update_payload["single_status"] = sd_result.get("single_status", "none")
                update_payload["single_sources"] = _json.dumps(sd_result.get("sources", []), default=str)
                update_payload["single_detection_last_updated"] = sd_now
                _sd_reasons = sd_result.get("reasons") or []
                _sd_d = sd_result.get("decision") or {}
                _sd_levels = _sd_d.get("source_levels") or {}
                _sd_diag = (
                    f", z=({_sd_d.get('album_z')},{_sd_d.get('artist_z')}), "
                    f"hi={_sd_d.get('high_sources')}, med={_sd_d.get('medium_sources')}, "
                    f"title={_sd_d.get('is_title_track')}, "
                    f"levels=(discogs={_sd_levels.get('discogs')},mb={_sd_levels.get('musicbrainz')},"
                    f"video={_sd_levels.get('discogs_video')},lastfm={_sd_levels.get('lastfm')})"
                    if _sd_d else ""
                )
                _sd_conf = str(sd_result.get("confidence", "low") or "low").upper()
                _sd_chips = _single_chips(sd_result.get("sources"))
                _single_summary = f"Single: {_sd_conf} {_sd_chips}".strip()
                logger.debug(
                    "[track_stage] %s - %s → single=%s (status=%s, sources=%d, reasons=%s%s)",
                    track_artist, track_title, sd_result.get("confidence", "low"),
                    sd_result.get("single_status", "none"),
                    len(sd_result.get("sources") or []),
                    ",".join(str(r) for r in _sd_reasons) or "none", _sd_diag,
                )

        except Exception as e:
            logger.debug("[track_stage][SINGLE] %s: %s", track_id, e)
            _single_summary = f"Single: ERROR ({e})"
            logger.warning("[track_stage] %s - %s → single detection ERROR: %s", track_artist, track_title, e)

    # -------------------------------------------------------------------------
    # 3. METADATA - MusicBrainz (via enrichment service for better matching)
    # -------------------------------------------------------------------------

    if not popularity_only and not singles_detection_only:
        try:
            title = _as_str(track.get("title"))
            artist = _as_str(track.get("artist"))

            if title and artist:
                # The per-track MusicBrainz lookup is the dominant per-track
                # API cost — skip it when the track already has a resolved
                # recording MBID and the scan isn't forced (metadata is
                # stable between scans). Mature frozen tracks also skip it —
                # the freeze is a score-only optimization, but the recording
                # metadata (MBID, album, writer) is stable once resolved.
                _has_mbid = bool(
                    _as_str(track.get("recording_mbid") or track.get("mbid") or track.get("musicbrainz_trackid"))
                )
                # Only columns that actually feed the genre display gate the
                # genre source fetches. ``musicbrainz_tags`` is NOT one of
                # them — the artist/album pages aggregate ``musicbrainz_genres``
                # and the other per-source columns, never tags. Including tags
                # here meant a track that only had MB tags never fetched MB
                # genres (and so never showed any genre on the artist page).
                _has_genres = bool(
                    track.get("musicbrainz_genres")
                    or track.get("discogs_genres")
                    or track.get("listenbrainz_genres")
                    or track.get("spotify_genres")
                    or track.get("lastfm_tags")
                )
                _force_meta = bool(options.get("force"))
                mb_data = None
                if frozen_track or (_has_mbid and not _force_meta):
                    logger.debug("[track_stage] Skipping MB metadata lookup for %s (frozen or MBID already resolved)", track_id)
                else:
                    # Album-level batch pre-resolution first — the runner
                    # resolved every fresh track of this album in one batched
                    # search, so only batch misses pay the per-track
                    # (search + recording lookup) request pair.
                    _batch_mb = options.get("mb_batch_metadata") or {}
                    mb_data = _batch_mb.get(f"{artist.lower()}::{title.lower()}")
                    if not mb_data:
                        mb_service = MusicBrainzService(http_client=get_shared_mb_client())
                        mb_data = mb_service.lookup_recording_metadata(title, artist)
                    if not mb_data:
                        logger.debug(
                            "[track_stage] MB metadata lookup returned nothing for %s (%s - %s)",
                            track_id, artist, title,
                        )

                if mb_data:
                    logger.debug(
                        "[track_stage] MB metadata for %s (%s - %s): mbid=%s confidence=%s title=%r album=%r",
                        track_id, artist, title,
                        mb_data.get("recording_mbid") or "-",
                        mb_data.get("confidence"),
                        mb_data.get("title"),
                        mb_data.get("album"),
                    )
                    recording_mbid = mb_data.get("recording_mbid")
                    confidence = mb_data.get("confidence")

                    if recording_mbid:
                        update_payload["recording_mbid"] = recording_mbid
                        update_payload["mbid"] = recording_mbid
                    if confidence is not None:
                        update_payload["musicbrainz_confidence"] = confidence

                    # Writer backfill from MusicBrainz work relationships
                    # (legacy composer lookup parity) — only when missing.
                    if recording_mbid:
                        _existing_writer = _as_str(track.get("writer") or "")
                        if not _existing_writer or _existing_writer.strip().lower() in ("[]", "null", "none", ""):
                            try:
                                writers = mb_service.get_composers_for_recording(recording_mbid)
                                if writers:
                                    import json
                                    update_payload["writer"] = json.dumps(writers)
                            except Exception as exc:
                                logger.debug("[track_stage][WRITER] %s: %s", track_id, exc)
                    if mb_data.get("title"):
                        update_payload["musicbrainz_title"] = mb_data["title"]
                    # Persist the resolved artist MBID (from the recording's
                    # artist-credit) so the single-detection service can use the
                    # reliable artist-scoped release-group search instead of
                    # falling back to fuzzier per-recording matching. Only set
                    # when the track doesn't already carry one — user edits win.
                    _artist_mbid = mb_data.get("artist_mbid")
                    if _artist_mbid and not _as_str(track.get("musicbrainz_artistid") or track.get("musicbrainz_artist_id")):
                        update_payload["musicbrainz_artistid"] = _artist_mbid
                    # ISRC pool backfill: recordings expose their ISRCs, so a
                    # track whose file tags lack one picks it up here — the
                    # ISRC then feeds the popularity fallback arms (Last.fm /
                    # ListenBrainz by-recording lookups) on later passes.
                    _mb_isrc = _as_str(mb_data.get("isrc") or "").strip()
                    if _mb_isrc and not _as_str(track.get("isrc") or "").strip():
                        update_payload["isrc"] = _mb_isrc
                    if mb_data.get("album"):
                        # Use the folder name from file_path as the primary
                        # reference for album matching — it reflects the actual
                        # file structure and is more reliable than the `album`
                        # column (which may have been overwritten by a previous
                        # bad MusicBrainz match). Falls back to the existing
                        # `album` column if file_path is not available.
                        existing_album = _as_str(track.get("album") or "")
                        fp = _as_str(track.get("file_path") or "")
                        folder_name = ""
                        if fp:
                            import os as _os
                            # Extract parent folder name from file path
                            # e.g. "/music/Artist/Album/track.flac" → "Album"
                            parts = _os.path.normpath(fp).split(_os.sep)
                            if len(parts) >= 2:
                                folder_name = parts[-2]
                        mb_album = _as_str(mb_data["album"])
                        match_ratio = 0.0
                        # An album column that already matches the folder is
                        # authoritative.  Per-track MusicBrainz releases for
                        # multi-edition albums ("OPVS NOIR Vol. 3" vs
                        # "OPVS NOIR Vol. 3 (Instrumental)") can resolve to a
                        # DIFFERENT release per track, and rewriting each
                        # track's album from its own lookup splits one folder
                        # into several albums on every metadata scan.  Only
                        # rewrite a folder-backed album when the column clearly
                        # disagrees with the folder (a previous bad MusicBrainz
                        # match) or when there is no folder to anchor on.
                        from difflib import SequenceMatcher
                        # Near-identical only: a loose threshold (0.6) also
                        # matches a sibling EDITION of the same release
                        # ("OPVS NOIR Vol. 3 (Instrumental)" vs "OPVS NOIR
                        # Vol. 3") and would both fail to repair a corrupted
                        # column and wrongly "confirm" a renamed track.
                        folder_consistent = bool(
                            folder_name
                            and existing_album
                            and SequenceMatcher(
                                None,
                                existing_album.lower(),
                                folder_name.lower(),
                            ).ratio() >= 0.9
                        )
                        if mb_album and not folder_consistent:
                            primary_ref = folder_name or existing_album
                            if primary_ref:
                                match_ratio = SequenceMatcher(None, primary_ref.lower(), mb_album.lower()).ratio()
                                if match_ratio >= 0.6:
                                    update_payload["album"] = mb_album
                                elif folder_name and existing_album:
                                    # If MB doesn't match the folder, check if
                                    # old album column is closer — keep
                                    # existing if so
                                    old_ratio = SequenceMatcher(None, existing_album.lower(), mb_album.lower()).ratio()
                                    if old_ratio >= 0.6:
                                        update_payload["album"] = mb_album
                            else:
                                update_payload["album"] = mb_album
                        elif folder_consistent:
                            logger.debug(
                                "[track_stage] Skipping album rename for %s (album '%s' matches folder '%s')",
                                track_id, existing_album, folder_name,
                            )

                        if not update_payload.get("album"):
                            logger.debug(
                                "[track_stage] Skipping album rename (folder='%s', album='%s') → '%s' (ratio=%.2f)",
                                folder_name or "?", existing_album, mb_album, match_ratio,
                            )
                    if mb_data.get("artist"):
                        update_payload["artist"] = mb_data["artist"]
                    if mb_data.get("year"):
                        update_payload["year"] = mb_data["year"]

            # Also fetch genre/tag data from MusicBrainz via genre-aware endpoint
            if title and artist and (not _has_genres or _force_meta):
                try:
                    mb_raw = get_shared_mb_client()
                    recs = mb_raw.search_recordings_with_genres(
                        f'artist:"{artist.replace(chr(34), "")}" AND recording:"{title.replace(chr(34), "")}"',
                        limit=3,
                    )
                    if recs:
                        rec = recs[0]
                        mb_genres = rec.get("genres") or []
                        mb_tags = rec.get("tags") or []
                        if mb_genres:
                            # Store as a JSON string — the column is TEXT and
                            # psycopg2 cannot adapt a Python list to a text
                            # parameter (this silently failed every track save).
                            update_payload["musicbrainz_genres"] = json.dumps(
                                [g.get("name", "") for g in mb_genres if g.get("name")],
                                ensure_ascii=False,
                            )
                        if mb_tags:
                            update_payload["musicbrainz_tags"] = json.dumps(
                                [t.get("name", "") for t in mb_tags if t.get("name")],
                                ensure_ascii=False,
                            )
                except Exception as e:
                    logger.debug("[track_stage][MB_GENRE] %s: %s", track_id, e)

            # Fetch Discogs genres for the track
            if title and artist and (not _has_genres or _force_meta):
                try:
                    from api_clients.discogs_http import DiscogsHttpClient
                    from helpers.config_helpers import get_config as _get_discogs_cfg
                    _discogs_cfg = (_get_discogs_cfg().get("api_integrations", {}) or {}).get("discogs", {}) or {}
                    _discogs_token = str(_discogs_cfg.get("token") or "").strip()
                    if not _discogs_token or _discogs_token.lower() in ("your_discogs_token", "your_token", "placeholder"):
                        _discogs_token = ""
                    if _discogs_token:
                        discogs = DiscogsHttpClient(token=_discogs_token)
                        results = discogs.search_database({
                            "q": f'{artist} {title}',
                            "type": "release",
                            "per_page": 3,
                        })
                        if results and len(results) > 0:
                            genres = results[0].get("genre", []) or []
                            styles = results[0].get("style", []) or []
                            if genres or styles:
                                update_payload["discogs_genres"] = json.dumps(
                                    list(set(genres + styles)),
                                    ensure_ascii=False,
                                )
                except Exception as e:
                    logger.debug("[track_stage][DISCOGS_GENRE] %s: %s", track_id, e)

            # Fetch ListenBrainz genres for the track (legacy parity) — only
            # when a recording MBID is known and the column is still empty.
            if title and artist and not track.get("listenbrainz_genres") and not update_payload.get("listenbrainz_genres"):
                try:
                    _lb_mbid = (
                        (mb_data or {}).get("recording_mbid")
                        or track.get("recording_mbid")
                        or track.get("mbid")
                        or track.get("musicbrainz_trackid")
                    )
                    if _lb_mbid:
                        from api_clients.listenbrainz import get_recording_tags
                        lb_tags = get_recording_tags(_lb_mbid) or []
                        names = [str(t.get("tag") or t.get("name") or "").strip() for t in lb_tags if isinstance(t, dict)]
                        names = [n for n in names if n]
                        if names:
                            update_payload["listenbrainz_genres"] = json.dumps(names, ensure_ascii=False)
                            logger.debug("[track_stage][LB_GENRE] %s: %d ListenBrainz genre(s)", track_id, len(names))
                except Exception as e:
                    logger.debug("[track_stage][LB_GENRE] %s: %s", track_id, e)

            # Fetch Spotify genres for the track (legacy parity) — artist-level
            # genres are cached per artist so a full scan only pays the lookup
            # once per artist instead of once per track.
            if title and artist and not track.get("spotify_genres") and not update_payload.get("spotify_genres"):
                try:
                    from services.enrichment.spotify_metadata_service import get_spotify_client
                    sp = get_spotify_client()
                    if sp is not None:
                        _sp_artist_key = (artist or "").strip().lower()
                        if _sp_artist_key not in _spotify_artist_genres_cache:
                            _sp_artist_id = sp.get_artist_id(artist)
                            _sp_genres: list[str] = []
                            if _sp_artist_id:
                                _sp_meta = sp.get_artist_metadata(_sp_artist_id) or {}
                                _sp_genres = [g for g in (_sp_meta.get("genres") or []) if g]
                            _spotify_artist_genres_cache[_sp_artist_key] = _sp_genres
                        _sp_genres = _spotify_artist_genres_cache.get(_sp_artist_key) or []
                        if _sp_genres:
                            update_payload["spotify_genres"] = json.dumps(_sp_genres, ensure_ascii=False)
                            logger.debug("[track_stage][SPOTIFY_GENRE] %s: %d genre(s)", track_id, len(_sp_genres))
                except Exception as e:
                    logger.debug("[track_stage][SPOTIFY_GENRE] %s: %s", track_id, e)

        except Exception as e:
            logger.debug("[track_stage][MB] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 4. COVER DETECTION (via enrichment service)
    # -------------------------------------------------------------------------

    if not popularity_only and not singles_detection_only:
        try:
            title = _as_str(effective_track.get("title") or track.get("title") or "")
            if title:
                # Pass existing DB cover state so already-confirmed covers
                # are skipped on subsequent scans (unless the scan options
                # indicate a forced re-check).
                raw_track = track_context.get("track", {}) if isinstance(track_context, dict) else {}
                cover_data = {
                    "is_cover": raw_track.get("is_cover") or track.get("is_cover"),
                    "original_cover_artist": raw_track.get("original_cover_artist") or "",
                    "cover_manual_override": raw_track.get("cover_manual_override") or track.get("cover_manual_override") or False,
                }
                force_cover = bool(options.get("force_cover_detection"))
                is_cover, reason = detect_cover_song(
                    title, track_artist,
                    track_data=cover_data,
                    force=force_cover,
                )
                if is_cover:
                    update_payload["is_cover"] = True
                    update_payload["is_cover_reason"] = reason
                    # Legacy parity: confirmed covers get the "Cover" genre
                    # prepended to their genre list (the old scanner injected
                    # it into musicbrainz_genres during the scan).
                    _mbg = update_payload.get("musicbrainz_genres")
                    if isinstance(_mbg, str):
                        try:
                            import json as _json
                            _mbg = _json.loads(_mbg)
                        except Exception:
                            _mbg = []
                    if isinstance(_mbg, list):
                        _cover_list = ["Cover"] + [g for g in _mbg if g != "Cover"]
                    else:
                        _cover_list = ["Cover"]
                    update_payload["musicbrainz_genres"] = json.dumps(_cover_list, ensure_ascii=False)
                    log_unified(f"[TRACK_STAGE] {track_artist} - {track_title} → cover detected ({reason})")
        except Exception as e:
            logger.debug("[track_stage][COVER] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 5. GENRE AGGREGATION (using enrichment service)
    # -------------------------------------------------------------------------
    # Genres are metadata — they are assigned during the metadata scan (and
    # full scans). A pure popularity-only pass skips them.

    if not popularity_only and not singles_detection_only:
        try:
            effective_track = _build_effective_track(track, update_payload)
            source_map = {}

            for key, source_name in [
                ("musicbrainz_genres", "musicbrainz"),
                ("discogs_genres", "discogs"),
                ("lastfm_tags", "lastfm"),
                ("listenbrainz_genres", "listenbrainz"),
                ("spotify_genres", "spotify"),
                ("essentia_genres", "essentia"),
            ]:
                raw = effective_track.get(key) or track.get(key) or ""
                if not raw:
                    continue
                import json
                try:
                    genres = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    genres = [g.strip() for g in str(raw).split(",") if g.strip()]
                if source_name == "essentia":
                    # Essentia writes "Parent---Child" style genres (e.g.
                    # "Rock---Heavy Metal; Rock---Punk") — keep the child part
                    # of each entry so specific genres win, top 3 only.
                    parsed: list[str] = []
                    for g in genres:
                        g = str(g).strip()
                        if "---" in g:
                            g = g.split("---", 1)[-1].strip()
                        if g and g not in parsed:
                            parsed.append(g)
                    genres = parsed[:3]
                if genres:
                    source_map[source_name] = genres

            if source_map:
                from services.enrichment.genre_aggregation_service import aggregate_genres
                # Top-3 genres across all sources.
                aggregated = aggregate_genres(source_map, max_genres=3)
                if aggregated:
                    # Persist to the REAL ``genres`` column (comma-joined, the
                    # format the old scanner wrote).  ``aggregated_genres`` is
                    # not a column and would be silently dropped by save_to_db.
                    update_payload["genres"] = ", ".join(aggregated)

                # Legacy parity: inject special genre tags (Christmas, Cover,
                # Live, Acoustic, Remix, ...) detected from the title/album —
                # the old scanner injected these during the scan and the
                # source APIs rarely provide them.
                try:
                    from services.metadata.genre_detector import detect_special_tags
                    _special = detect_special_tags(
                        track_name=_as_str(effective_track.get("title") or track.get("title") or ""),
                        album_name=_as_str(album_context.get("album") or track.get("album") or ""),
                        artist_genres=None,
                        audio_features=None,
                        album_type=_as_str(album_result.get("detected_album_type") or options.get("album_type") or "") or None,
                    )
                except Exception:
                    _special = set()
                if _special:
                    _existing = update_payload.get("genres") or ""
                    if isinstance(_existing, (list, tuple)):
                        _existing = ", ".join(str(x) for x in _existing)
                    _merged = [g.strip() for g in str(_existing).split(",") if g.strip()]
                    for _tag in sorted(_special):
                        if _tag not in _merged:
                            _merged.append(_tag)
                    update_payload["genres"] = ", ".join(_merged)
                    logger.debug(
                        "[track_stage] Special genre tags for %s: %s",
                        track_id, sorted(_special),
                    )

        except Exception as e:
            logger.debug("[track_stage][GENRE] %s: %s", track_id, e)

    # -------------------------------------------------------------------------
    # 6. PERSISTENCE
    # -------------------------------------------------------------------------

    effective_track = _strip_album_type_columns(track, update_payload)

    try:
        insert_or_update_track(track_id, effective_track)
    except Exception as e:
        # Surface persistence failures — a silent drop here means scores,
        # single status and metadata never reach the DB while the unified
        # log still looks healthy (results are returned from memory).
        logger.warning("[track_stage][DB] Persist failed for %s: %s", track_id, e)
        try:
            log_unified(
                f"[TRACK_STAGE] {track_artist} - {track_title} → DB persist FAILED: {e}"
            )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # 7. RETURN RESULT
    # -------------------------------------------------------------------------

    # Return the ADJUSTED score so the result dict matches what was persisted:
    # ``update_payload["final_score"]`` carries the artist-context and
    # album-deviation adjustments, while ``score_data["combined_score"]`` is
    # raw.  Mixing raw scan scores with stored adjusted ``final_score`` values
    # in the album/artist distributions skews every z-score.  Falls back to the
    # raw value for paths that never ran adjustments (singles-only, cache).
    _result_final_score = float(
        update_payload.get("final_score") or score_data.get("combined_score") or 0
    )

    # Grouping key for the finaliser: albums must be keyed by the ALBUM artist,
    # never the per-track artist.  A "Feuerschwanz feat. Fabienne Erni" track on
    # the album "Fegefeuer" belongs to the SAME album distribution as its
    # album-mates — grouping by the raw track artist split the album into N
    # "Fegefeuer by <feat.>" fragments in memory (tracks=1, MAD=0.0 → broken
    # z-scores + duplicate Navidrome syncs).
    _album_artist = _as_str(
        track.get("album_artist")
        or album_context.get("album_artist")
        or album_context.get("artist")
        or track_artist
    )

    # ── Consolidated per-track log line ───────────────────────────────────
    # One line per track in the unified log: title, popularity score with
    # provider counts, ISRC, and the single verdict with source chips.  The
    # metadata pre-pass (Pass 1 of a combined scan) logs at DEBUG only so the
    # scan output is not printed twice for every track.
    if not _single_summary:
        _stored_conf = str(
            update_payload.get("single_confidence")
            or track.get("single_confidence")
            or "low"
        ).upper()
        _single_summary = f"Single: {_stored_conf} (stored)"
    _isrc_part = f" | ISRC: {_isrc_found}" if _isrc_found else ""
    _consolidated = (
        f"[TRACK] 🎵 \"{str(track_title or '').strip()}\""
        f" | {_pop_summary or 'Score: —'}"
        f"{_isrc_part}"
        f" | {_single_summary}"
    )
    if metadata_only:
        logger.debug("[TRACK_STAGE] %s", _consolidated)
    else:
        log_unified(_consolidated)

    return {
        "track_id": track_id,
        "artist": track_artist,
        "album_artist": _album_artist,
        "album": track.get("album") or effective_track.get("album", ""),
        # ``_strip_album_type_columns`` drops the stale title (the album
        # stage owns renames), but the in-memory loaded title is the correct
        # display value for the result/summary.
        "title": track.get("title") or effective_track.get("title") or "",
        "lastfm_listeners": int(lastfm_listeners or 0),
        "listenbrainz_listens": int(listenbrainz_listens or 0),
        "lb_percentile": float(lb_percentile or 0.0),
        "popularity_score": _result_final_score,
        "final_score": _result_final_score,
        "_raw_combined": float(update_payload.get("_raw_combined") or 0),
        "spotify_score": float(score_data.get("spotify_score", 0)),
        "lastfm_score": float(score_data.get("lastfm_score", 0)),
        "listenbrainz_score": float(score_data.get("listenbrainz_score", 0)),
        "is_single": bool(update_payload.get("is_single", track.get("is_single", False))),
        "single_confidence": str(update_payload.get("single_confidence", track.get("single_confidence", "low"))),
        "single_sources": update_payload.get("single_sources", track.get("single_sources", "")),
        "popularity_marked": bool(track.get("popularity_marked", False)),
        "is_live": bool(
            track.get("is_live")
            or track.get("album_context_live")
            or is_live_or_alternate_track_title(track.get("title"))
        ),
        "exclude_from_stats": bool(track.get("exclude_from_stats")),
    }