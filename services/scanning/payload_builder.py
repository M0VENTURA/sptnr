"""Build DB-ready track payloads from Navidrome track data.

Constructs database-ready payload dicts from raw Navidrome track data
and extracted metadata. Supports both legacy and new call patterns.

Key Functions:
    - build_track_payload(): Build a complete DB insert payload from
      Navidrome track data and extracted metadata fields.

Call Patterns:
    Legacy: build_track_payload(track=..., extracted=..., writer_json=...)
    New:    build_track_payload(track=..., get_song=client.get_song)

In the new path, metadata extraction is delegated to
``services.scanning.metadata_extractor`` instead of ``api_clients.navidrome``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from helpers.musicbrainz_helpers import normalize_single_mbid
from helpers.normalization_service import clean_artist_name_for_storage
from services.scanning.metadata_extractor import extract_track_metadata

LOCAL_TZ = os.environ.get("TIMEZONE") or os.environ.get("TZ") or "UTC"
JSON_EMPTY_LIST = json.dumps([])

NAVIDROME_SCORE_DEFAULTS = {
    "score": 0.0,
    "spotify_score": 0,
    "lastfm_score": 0,
    "listenbrainz_score": 0,
    "age_score": 0,
    "spotify_genres": JSON_EMPTY_LIST,
    "lastfm_tags": JSON_EMPTY_LIST,
    "discogs_genres": JSON_EMPTY_LIST,
    "audiodb_genres": JSON_EMPTY_LIST,
    "musicbrainz_genres": JSON_EMPTY_LIST,
    "spotify_album": "",
    "spotify_artist": "",
    "spotify_popularity": 0,
    "spotify_album_art_url": "",
    "lastfm_track_playcount": 0,
    "spotify_total_tracks": 0,
    "spotify_id": None,
    "is_spotify_single": 0,
    "is_single": False,
    "single_confidence": "low",
    "single_sources": JSON_EMPTY_LIST,
    "suggested_mbid": "",
    "suggested_mbid_confidence": 0.0,
}

EXTRACTED_STRING_FIELDS = (
    "mbid", "musicbrainz_albumid", "musicbrainz_trackid", "musicbrainz_releasegroupid",
    "musicbrainz_releasetrackid", "musicbrainz_albumstatus", "musicbrainz_albumtype",
    "musicbrainz_releasecountry", "musicbrainz_albumartistid", "musicbrainz_workid",
    "releasetype", "releasestatus", "releasecountry", "media", "label", "recordlabel",
    "tracktotal", "disctotal", "compilation", "grouping", "albumversion", "discsubtitle",
    "script", "replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain",
    "replaygain_album_peak", "r128_track_gain", "r128_album_gain", "releasedate",
    "originalyear", "originaldate", "copyright", "barcode", "catalognumber", "asin",
    "subtitle", "lyrics", "language", "work", "movement", "movementname", "movementtotal",
    "key", "explicitstatus", "composer", "lyricist", "conductor", "remixer", "producer",
    "arranger", "mixer", "engineer", "director", "djmixer", "performer", "titlesort",
    "albumsort", "artistsort", "albumartistsort", "albumartistssort", "artistssort",
    "composersort", "lyricistsort", "artists", "albumartists", "encodedby", "encodersettings",
    "website", "license", "isrc", "comment",
)

EXTRACTED_DIRECT_FIELDS = (
    "bpm", "danceability", "stars", "duration", "track_number", "disc_number", "year",
    "bitrate", "sample_rate",
)


def now_local_iso() -> str:
    """Return an ISO timestamp in the configured local timezone."""
    try:
        return datetime.now(ZoneInfo(LOCAL_TZ)).isoformat()
    except Exception:
        return datetime.now().isoformat()


def build_track_payload(
    *,
    track: dict[str, Any],
    album_name: str,
    album_artist_value: str,
    canonical_artist_name: str,
    extracted: dict[str, Any] | None = None,
    album_context: dict[str, Any] | None = None,
    writer_json: str | None = None,
    get_song: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a DB-ready payload for a Navidrome track.

    Args:
        track: Raw Navidrome track object.
        album_name: Album name from current album context.
        album_artist_value: Resolved album artist value.
        canonical_artist_name: Clean fallback artist name.
        extracted: Optional already-extracted metadata. Existing callers can
            keep passing this.
        album_context: Optional live/unplugged context flags.
        writer_json: Optional writer JSON override. Existing callers can keep
            passing this.
        get_song: Optional getSong callback for extractor fallback.
    """
    album_context = album_context or {}
    extracted = extracted or extract_track_metadata(track, get_song=get_song)
    writer_json = writer_json if writer_json is not None else extracted.get("writer", "[]") or "[]"

    track_artist = (
        clean_artist_name_for_storage(track.get("artist", "") or canonical_artist_name)
        or canonical_artist_name
    )

    payload: dict[str, Any] = {
        "_navidrome_sync": True,
        "id": track.get("id"),
        "title": track.get("title", ""),
        "album": album_name,
        "artist": track_artist,
        "album_artist": album_artist_value,
        "last_scanned": now_local_iso(),
        "genres": extracted.get("navidrome_genres", "") or "",
        "navidrome_genres": extracted.get("navidrome_genres", "") or "",
        "navidrome_genre": extracted.get("navidrome_genre", "") or "",
        "file_path": extracted.get("file_path", "") or "",
        "spotify_release_date": extracted.get("year", "") or "",
        "musicbrainz_album_mbid": extracted.get("musicbrainz_albumid", "") or "",
        "musicbrainz_artistid": normalize_single_mbid(extracted.get("musicbrainz_artistid", "") or ""),
        "musicbrainz_artist_id": normalize_single_mbid(
            extracted.get("musicbrainz_artist_id", "")
            or extracted.get("musicbrainz_artistid", "")
            or ""
        ),
        "writer": writer_json,
        "album_context_live": 1 if album_context.get("is_live") else 0,
        "album_context_unplugged": 1 if album_context.get("is_unplugged") else 0,
    }

    payload.update(NAVIDROME_SCORE_DEFAULTS)

    for field in EXTRACTED_STRING_FIELDS:
        payload[field] = extracted.get(field, "") or ""

    for field in EXTRACTED_DIRECT_FIELDS:
        payload[field] = extracted.get(field)

    return payload
