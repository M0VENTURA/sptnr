"""Popularity scan hook helpers.

This module connects the scan pipeline to the shared classification and
normalisation services.

It keeps the main scanner clean by centralising:
- lookup title normalization
- Last.fm lookup normalization
- cover-title cleanup
- remaster-only detection
- album classification
- track stat-exclusion flags
"""

from __future__ import annotations

from typing import Any

from services.catalog.album_classification_service import (
    classify_compilation_category,
    detect_alternate_takes,
    detect_greatest_hits_album,
    detect_live_album_type,
    is_live_or_alternate_album,
    normalize_primary_release_type,
    should_exclude_track_from_stats,
)

from helpers.normalization_service import (
    detect_cover_and_normalize_title,
    is_remastered_only_variant,
    normalize_title_for_lastfm,
    normalize_title_for_lookup,
    strip_remaster_suffix,
    strip_single_release_suffix,
)


def _duration_seconds(value: Any) -> float | None:
    """Best-effort track duration in seconds (None when unknown/zero)."""
    try:
        v = float(value or 0)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def prepare_album_context(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
    musicbrainz_album_type: str | None = None,
) -> dict[str, Any]:

    primary_release_type = normalize_primary_release_type(
        spotify_album_type or musicbrainz_album_type or ""
    )

    album_type_from_field = spotify_album_type or musicbrainz_album_type or ""

    live_album_type = detect_live_album_type(
        album,
        album_type_from_field=album_type_from_field,
    )

    # The album-type field (MusicBrainz/Spotify match) is authoritative for
    # the live verdict; the title-based check only fills in for albums with
    # NO matched album type.
    is_live_album = bool(live_album_type) or (
        not album_type_from_field and is_live_or_alternate_album(album)
    )

    # Compilation classification is split into VA (per-track artist context
    # required) vs single-artist (Greatest Hits — treated like a studio
    # album).  ``is_compilation`` keeps the boolean "any compilation" view
    # for legacy consumers; downstream stages use the subtype flags.
    comp_category = classify_compilation_category(
        artist=artist,
        album=album,
        tracks=tracks,
        album_artist=album_artist,
        spotify_album_type=spotify_album_type,
        musicbrainz_album_type=musicbrainz_album_type,
    )
    is_compilation = comp_category != ""
    is_va_compilation = comp_category == "va"
    is_single_artist_compilation = comp_category == "single_artist"

    is_greatest_hits = detect_greatest_hits_album(album=album, artist=artist)

    alternate_takes = detect_alternate_takes(tracks)

    return {
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "primary_release_type": primary_release_type,
        "spotify_album_type": spotify_album_type,
        "musicbrainz_album_type": musicbrainz_album_type,
        "live_album_type": live_album_type,
        "is_live_album": is_live_album,
        "is_compilation": is_compilation,
        "is_va_compilation": is_va_compilation,
        "is_single_artist_compilation": is_single_artist_compilation,
        "is_greatest_hits": is_greatest_hits,
        "alternate_takes": alternate_takes,
    }


def prepare_track_context(
    track: dict[str, Any],
    album_context: dict[str, Any] | None = None,
) -> dict[str, Any]:

    album_context = album_context or {}

    title = track.get("title") or track.get("name") or ""
    artist = (
        track.get("artist")
        or track.get("album_artist")
        or album_context.get("artist")
        or ""
    )
    album = track.get("album") or album_context.get("album") or ""

    is_cover, cover_normalized_title = detect_cover_and_normalize_title(title)

    lookup_title = normalize_title_for_lookup(title)
    lastfm_title = normalize_title_for_lastfm(title)

    remaster_stripped_title = strip_remaster_suffix(title)
    single_suffix_stripped_title = strip_single_release_suffix(title)

    remastered_only = is_remastered_only_variant(title)

    is_live = int(track.get("is_live") or 0)
    album_context_live = 1 if album_context.get("is_live_album") else 0

    exclude_from_stats = should_exclude_track_from_stats(
        title=title,
        album=album,
        is_live=is_live,
        album_context_live=album_context_live,
        album_type=album_context.get("musicbrainz_album_type")
        or album_context.get("spotify_album_type")
        or "",
        duration=_duration_seconds(track.get("duration")),
    )

    return {
        "track": track,
        "title": title,
        "artist": artist,
        "album": album,

        "lookup_title": lookup_title,
        "lastfm_title": lastfm_title,
        "cover_normalized_title": cover_normalized_title,
        "remaster_stripped_title": remaster_stripped_title,
        "single_suffix_stripped_title": single_suffix_stripped_title,

        "is_cover": is_cover,
        "is_remastered_only_variant": remastered_only,
        "exclude_from_stats": exclude_from_stats,
        "album_context_live": album_context_live,

        "album_context": album_context,
    }


def prepare_tracks_for_album(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
    musicbrainz_album_type: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:

    album_context = prepare_album_context(
        artist=artist,
        album=album,
        tracks=tracks,
        album_artist=album_artist,
        spotify_album_type=spotify_album_type,
        musicbrainz_album_type=musicbrainz_album_type,
    )

    track_contexts = [
        prepare_track_context(track, album_context)
        for track in tracks or []
    ]

    return album_context, track_contexts


def get_stat_eligible_tracks(track_contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        ctx["track"]
        for ctx in track_contexts or []
        if not ctx.get("exclude_from_stats")
    ]


def apply_context_fields_to_track(track_context: dict[str, Any]) -> dict[str, Any]:
    track = dict(track_context.get("track") or {})

    track.update({
        "lookup_title": track_context.get("lookup_title"),
        "lastfm_title": track_context.get("lastfm_title"),
        "cover_normalized_title": track_context.get("cover_normalized_title"),
        "is_cover": track_context.get("is_cover"),
        "is_remastered_only_variant": track_context.get("is_remastered_only_variant"),
        "exclude_from_stats": track_context.get("exclude_from_stats"),
        "album_context_live": track_context.get("album_context_live"),
    })

    return track


__all__ = [
    "normalize_title_for_lastfm",
    "normalize_title_for_lookup",
    "detect_cover_and_normalize_title",
    "is_remastered_only_variant",
    "strip_remaster_suffix",
    "strip_single_release_suffix",
    "prepare_album_context",
    "prepare_track_context",
    "prepare_tracks_for_album",
]
