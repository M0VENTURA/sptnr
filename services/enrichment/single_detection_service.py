"""Single detection service.

Single detection is metadata/enrichment classification, not popularity math.
Popularity scans may call this service, but should treat the result as an
external classification signal rather than embedding single-detection rules.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from helpers.normalization_service import strip_single_release_suffix, normalize_title_for_lookup

logger = logging.getLogger(__name__)

IGNORE_SINGLE_KEYWORDS = [
    "intro", "outro", "jam",
    "live", "unplugged",
    "remix", "edit", "mix",
    "acoustic", "orchestral",
    "demo", "instrumental", "karaoke",
]


def should_skip_single_detection(title: str, album_type: str | None = None) -> bool:
    """Return True for obvious non-single alternate/live/demo versions."""
    title_l = (title or "").lower()
    album_type_l = (album_type or "").lower()
    if any(keyword in title_l for keyword in IGNORE_SINGLE_KEYWORDS):
        # Remaster/remastered intentionally not listed; it should remain eligible.
        return True
    if any(keyword in album_type_l for keyword in ["live", "compilation"]):
        return True
    return False


def _source_result(source: str, matched: bool, confidence: float = 0.0, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "matched": bool(matched),
        "confidence": round(float(confidence or 0.0), 3),
        "metadata": metadata or {},
    }


def _detect_musicbrainz(title: str, artist: str, artist_mbid: str | None, album_track_count: int | None, mb_client=None) -> dict[str, Any]:
    try:
        if mb_client is None:
            from api_clients.musicbrainz_http import MusicBrainzClient
            mb_client = MusicBrainzClient()
        matched = bool(mb_client.is_single(title, artist, artist_mbid=artist_mbid, album_track_count=album_track_count))
        release_date = None
        if matched and hasattr(mb_client, "get_single_release_date"):
            release_date = mb_client.get_single_release_date(title, artist, artist_mbid=artist_mbid)
        return _source_result("musicbrainz", matched, 0.9 if matched else 0.0, {"release_date": release_date} if release_date else {})
    except Exception as exc:
        logger.debug("MusicBrainz single detection failed for %s / %s: %s", artist, title, exc)
        return _source_result("musicbrainz", False)


def _detect_discogs(title: str, artist: str, album: str | None, discogs_token: str | None, duration: float | None = None) -> dict[str, Any]:
    token = discogs_token or os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        return _source_result("discogs", False)
    try:
        from services.enrichment.discogs_service import DiscogsService
        service = DiscogsService(token=token)
        album_context = {"album": album} if album else None
        matched = bool(service.is_single(title, artist, album_context=album_context))
        release_year = service.get_single_release_year(title, artist) if matched and hasattr(service, "get_single_release_year") else None
        return _source_result("discogs", matched, 0.8 if matched else 0.0, {"release_year": release_year} if release_year else {})
    except Exception as exc:
        logger.debug("Discogs single detection failed for %s / %s: %s", artist, title, exc)
        return _source_result("discogs", False)


def _detect_spotify(title: str, artist: str, spotify_results_cache: dict | None = None) -> dict[str, Any]:
    """Best-effort Spotify single signal using caller-provided cache/results."""
    if not spotify_results_cache:
        return _source_result("spotify", False)
    try:
        normalized = normalize_title_for_lookup(strip_single_release_suffix(title)).lower()
        for item in spotify_results_cache.get((artist, title), []) if isinstance(spotify_results_cache, dict) else []:
            item_title = (item.get("name") or item.get("title") or "").lower()
            album_type = (item.get("album_type") or item.get("type") or "").lower()
            if normalized and normalize_title_for_lookup(item_title).lower() == normalized and album_type == "single":
                return _source_result("spotify", True, 0.75, {"album_type": album_type})
    except Exception:
        pass
    return _source_result("spotify", False)


def detect_single_for_track(
    title: str,
    artist: str,
    album_track_count: int = 1,
    spotify_results_cache: dict | None = None,
    verbose: bool = False,
    discogs_token: str | None = None,
    track_id: str | None = None,
    album: str | None = None,
    isrc: str | None = None,
    duration: float | None = None,
    popularity: float | None = None,
    album_type: str | None = None,
    use_advanced_detection: bool = True,
    zscore_threshold: float = 1.0,
    album_is_underperforming: bool = False,
    artist_median_popularity: float = 0.0,
    lastfm_client=None,
    track_repo: Any = None,  # Compliant repository parameter
    persist_result: bool = True,
    mb_cached_singles: set | None = None,
    artist_mbid: str | None = None,
    mb_client=None,
) -> dict[str, Any]:
    """Detect whether a track is a single using multiple source signals.

    Returns a stable dict shape suitable for persistence/logging:
    ``is_single``, ``confidence``, ``sources``, ``reasons``.
    """
    title = title or ""
    artist = artist or ""
    lookup_title = strip_single_release_suffix(title)
    logger.debug("[SINGLE_DETECTION] Checking: %s - %s", artist, title)

    if not title or not artist:
        return {"is_single": False, "confidence": 0.0, "sources": [], "reasons": ["missing_title_or_artist"]}

    if album_track_count is not None and album_track_count >= 4 and not use_advanced_detection:
        return {"is_single": False, "confidence": 0.0, "sources": [], "reasons": ["album_track_count_not_single_candidate"]}

    if should_skip_single_detection(title, album_type=album_type):
        return {"is_single": False, "confidence": 0.0, "sources": [], "reasons": ["alternate_or_live_version"]}

    source_results = [
        _detect_musicbrainz(lookup_title, artist, artist_mbid, album_track_count, mb_client=mb_client),
        _detect_discogs(lookup_title, artist, album, discogs_token, duration=duration),
        _detect_spotify(lookup_title, artist, spotify_results_cache=spotify_results_cache),
    ]

    matched = [item for item in source_results if item.get("matched")]
    confidence = max([item.get("confidence", 0.0) for item in matched], default=0.0)

    # Popularity/z-score should only be a supporting hint, never the only
    # reason a track becomes a single. This prevents popularity logic from
    # owning single detection.
    reasons = [f"{item['source']}_matched" for item in matched]
    if matched and popularity and artist_median_popularity and popularity >= artist_median_popularity:
        confidence = min(1.0, confidence + 0.05)
        reasons.append("popularity_supporting_signal")

    result = {
        "is_single": bool(matched),
        "confidence": round(confidence, 3),
        "sources": source_results,
        "reasons": reasons or ["no_source_match"],
    }

    # *** COMPLIANT PERSISTENCE INTERFACE ***
    if persist_result and track_repo and track_id:
        try:
            # We now call the Repository Interface rather than
            # executing raw SQL internally.
            track_repo.update_track_single_status(
                track_id,
                result["is_single"],
                result["confidence"]
            )
        except Exception as exc:
            logger.debug(
                "Compliant persistence interface failed for %s: %s",
                track_id, exc
            )

    return result