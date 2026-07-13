"""Album enrichment/statistics stage.

Orchestrates album-level enrichment during a popularity scan, delegating to
existing ``services.enrichment.*`` and ``services.metadata.*`` modules instead
of duplicating their logic.

Handles:
- Album type detection (compilation, live, remix overrides)
- Album art lookup with fallback → delegates to ``album_art_service``
- Artist metadata (country, bio, image) → delegates to ``artist_metadata_service``
- Similar artists fetching and caching
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get

# ── Existing enrichment/metadata services (not duplicated) ─────────────────
from services.enrichment.album_art_service import (
    save_album_art_to_db,
    fetch_album_art_from_musicbrainz,
)
from services.enrichment.artist_bio_service import get_artist_biography
from services.metadata.artist_metadata_service import (
    cleanup_false_positive_missing,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Album type detection (orchestration logic — no existing service covers this)
# ---------------------------------------------------------------------------

_COMPILATION_ARTISTS = frozenset(["various artists", "various artists -", "various", "compilation", "soundtrack"])
_HETEROGENEOUS_MARKERS = [
    "+compilation", "(compilation)", "+soundtrack", "(soundtrack)",
    "+live", "(live)", "+remix", "(remix)", "+spokenword", "(spokenword)",
]
_LIVE_ALBUM_PATTERNS = [
    r"\blive\s+at\b", r"\blive\s+in\b", r"\blive\s+from\b",
    r"\blive\s+session\b", r"\(live\)\s*$", r"\bunplugged\b", r"\bacoustic\b",
]


def _detect_album_type(artist: str, album: str, album_artist: str | None, spotify_type: str | None) -> str:
    """Detect album type (album, compilation, live, remix, etc.)."""
    artist_lower = artist.lower()
    album_lower = album.lower()

    if artist_lower in _COMPILATION_ARTISTS:
        return "album+compilation"
    if album_artist and album_artist.lower() in _COMPILATION_ARTISTS:
        return "album+compilation"

    if spotify_type:
        st = spotify_type.lower()
        if st == "compilation" or "+compilation" in st or "(compilation)" in st:
            return "album+compilation"

    if "soundtrack" in album_lower:
        return "album+soundtrack"

    import re
    if any(re.search(p, album_lower) for p in _LIVE_ALBUM_PATTERNS):
        return "album+live"

    if "+remix" in album_lower or "(remix)" in album_lower:
        return "album+remix"

    return "album"


# ---------------------------------------------------------------------------
# Album art helper (uses existing services, adds fallback orchestration)
# ---------------------------------------------------------------------------

def _fetch_album_art_with_fallback(artist: str, album: str, discogs_token: str | None = None) -> str | None:
    """Fetch album art trying MusicBrainz/CAA first, then AudioDB, then Discogs.

    Uses the existing ``album_art_service`` functions for CAA; falls back to
    AudioDB and Discogs manually since those paths are not yet covered by the
    enrichment layer.
    """
    # 1) Try MusicBrainz / Cover Art Archive via existing service
    try:
        data = fetch_album_art_from_musicbrainz(artist, album)
        if data:
            save_album_art_to_db(artist, album, data, source="musicbrainz")
            return "musicbrainz"
    except Exception as exc:
        logger.debug("[album_stage] CAA fetch failed: %s", exc)

    # 2) Try AudioDB via existing helper
    try:
        from api_clients.audiodb import get_album_artwork
        art_url = get_album_artwork(artist, album, enabled=True)
        if art_url:
            resp = httpx.get(art_url, timeout=10)
            if resp.status_code == 200 and resp.content:
                save_album_art_to_db(artist, album, resp.content, source="audiodb")
                return "audiodb"
    except Exception as exc:
        logger.debug("[album_stage] AudioDB art failed: %s", exc)

    # 3) Try Discogs
    try:
        from api_clients.discogs_http import DiscogsHttpClient
        if discogs_token and len(discogs_token) >= 10 and discogs_token.lower() not in ("your_discogs_token", "your_token", "placeholder"):
            client = DiscogsHttpClient(discogs_token)
            # Use the existing discogs_http-based search
            results = client.search_album_release(artist, album)
            if results and results[0].get("cover_image"):
                resp = httpx.get(results[0]["cover_image"], timeout=10)
                if resp.status_code == 200 and resp.content:
                    save_album_art_to_db(artist, album, resp.content, source="discogs")
                    return "discogs"
    except Exception as exc:
        logger.debug("[album_stage] Discogs art failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Artist metadata helper (delegates to existing enrichment services)
# ---------------------------------------------------------------------------

def _fetch_artist_metadata(artist: str, conn) -> dict[str, Any]:
    """Fetch artist country, bio, and image, delegating to existing services."""
    result: dict[str, Any] = {"country": None, "bio": None, "image_url": None}
    cursor = conn.cursor()

    cursor.execute("SELECT country, bio, image_url FROM artists WHERE name = %s", (artist,))
    existing = cursor.fetchone()
    if existing:
        result = {
            "country": row_get(existing, "country") or None,
            "bio": row_get(existing, "bio") or None,
            "image_url": row_get(existing, "image_url") or None,
        }

    # Bio via existing enrichment service
    if not result["bio"]:
        try:
            bio = get_artist_biography(artist)
            result["bio"] = str(bio) if bio else None
        except Exception as exc:
            logger.debug("[album_stage] Bio lookup failed: %s", exc)

    # Country via MusicBrainz HTTP client
    if not result["country"]:
        try:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            mb = MusicBrainzHttpClient()
            country = mb.get_artist_country(artist)
            result["country"] = str(country) if country else None
        except Exception as exc:
            logger.debug("[album_stage] Country lookup failed: %s", exc)

    # Image via AudioDB
    if not result["image_url"]:
        try:
            from api_clients.audiodb import get_artist_fanart
            img = get_artist_fanart(artist, enabled=True)
            result["image_url"] = str(img) if img else None
        except Exception as exc:
            logger.debug("[album_stage] Image lookup failed: %s", exc)

    # Persist
    try:
        cursor.execute("""
            INSERT INTO artists (id, name, country, bio, image_url)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                country = COALESCE(excluded.country, artists.country),
                bio = COALESCE(excluded.bio, artists.bio),
                image_url = COALESCE(excluded.image_url, artists.image_url)
        """, (artist, artist, result["country"], result["bio"], result["image_url"]))
        conn.commit()
    except Exception as exc:
        logger.debug("[album_stage] Persist artist metadata failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Similar artists helper (no existing service — kept here for now)
# ---------------------------------------------------------------------------

def _fetch_similar_artists(artist: str, conn, options: dict) -> dict[str, list]:
    """Fetch and cache similar artists from Last.fm (and ListenBrainz in future)."""
    result: dict[str, list] = {"lastfm": [], "listenbrainz": []}
    cursor = conn.cursor()

    if bool(options.get("singles_only")) or bool(options.get("singles_with_missing_popularity")):
        return result

    try:
        cursor.execute("""
            SELECT similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated
            FROM artists WHERE name = %s
        """, (artist,))
        cached = cursor.fetchone()
        if cached:
            lf_raw = row_get(cached, "similar_artists_lastfm")
            lb_raw = row_get(cached, "similar_artists_listenbrainz")
            ts_raw = row_get(cached, "similar_artists_last_updated")
            if lf_raw:
                result["lastfm"] = json.loads(lf_raw)
            if lb_raw:
                result["listenbrainz"] = json.loads(lb_raw)
            if (result["lastfm"] or result["listenbrainz"]) and ts_raw:
                try:
                    ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - ts).days
                    if age < 90:
                        return result
                except Exception:
                    pass

        from helpers.config_helpers import get_config
        cfg = get_config()
        lf_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
        if lf_cfg.get("enabled") and lf_cfg.get("api_key"):
            from api_clients.lastfm import LastFmClient
            lf = LastFmClient(lf_cfg["api_key"])
            similar = lf.get_similar_artists(artist, limit=10) or []
            result["lastfm"] = [s.get("name", "") for s in similar if isinstance(s, dict)]

        cursor.execute("""
            INSERT INTO artists (id, name, similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                similar_artists_lastfm = excluded.similar_artists_lastfm,
                similar_artists_listenbrainz = excluded.similar_artists_listenbrainz,
                similar_artists_last_updated = excluded.similar_artists_last_updated
        """, (artist, artist, json.dumps(result["lastfm"]) if result["lastfm"] else None, None, datetime.now().isoformat()))
        conn.commit()
    except Exception as exc:
        logger.debug("[album_stage] Similar artists failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def enrich_album(
    *,
    album_row: dict[str, Any],
    album_context: dict[str, Any],
    stat_eligible_tracks: list[dict[str, Any]],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Run album-level enrichment.

    Returns a dict with enrichment results that the caller can pass downstream
    to per-track processing and finalisation stages.
    """
    artist = str(album_row.get("artist") or "")
    album = str(album_row.get("album") or "")
    album_artist = str(album_row.get("album_artist") or "")
    spotify_type = str(album_row.get("spotify_album_type") or "")

    # Acquire Discogs token from config
    discogs_token: str | None = None
    try:
        from helpers.config_helpers import get_config
        cfg = get_config()
        discogs_token = cfg.get("api_integrations", {}).get("discogs", {}).get("token")
        if discogs_token and discogs_token.lower() in ("your_discogs_token", "your_token", "placeholder", ""):
            discogs_token = None
    except Exception:
        pass

    conn = get_db_connection()
    try:
        # 1. Album type detection
        detected_type = _detect_album_type(artist, album, album_artist or None, spotify_type or None)
        is_hetero = any(m in detected_type.lower() for m in _HETEROGENEOUS_MARKERS)
        logger.info("[album_stage] '%s - %s' → type=%s, heterogeneous=%s",
                     artist, album, detected_type, is_hetero)

        # 2. Album art (delegates to existing enrichment services)
        art_source = _fetch_album_art_with_fallback(artist, album, discogs_token)
        if art_source:
            logger.info("[album_stage] Album art cached for %s - %s (%s)", artist, album, art_source)

        # 3. Artist metadata (delegates to existing enrichment services)
        meta = _fetch_artist_metadata(artist, conn)

        # 4. Similar artists
        similar = _fetch_similar_artists(artist, conn, options)

        conn.commit()
    except Exception as exc:
        logger.error("[album_stage] Enrichment failed for '%s - %s': %s", artist, album, exc)
        try:
            conn.rollback()
        except Exception:
            pass
        detected_type = "album"
        is_hetero = False
        art_source = None
        meta = {"country": None, "bio": None, "image_url": None}
        similar = {"lastfm": [], "listenbrainz": []}
    finally:
        conn.close()

    extra_context = {}
    if meta.get("country"):
        extra_context["artist_country"] = meta["country"]
    if similar["lastfm"]:
        extra_context["similar_artists_lastfm"] = similar["lastfm"]
    if similar["listenbrainz"]:
        extra_context["similar_artists_listenbrainz"] = similar["listenbrainz"]

    return {
        "album_row": album_row,
        "album_context": {**album_context, **extra_context},
        "stat_eligible_tracks": stat_eligible_tracks,
        "detected_album_type": detected_type,
        "is_heterogeneous": is_hetero,
        "similar_artists": similar,
        "artist_metadata": meta,
    }

