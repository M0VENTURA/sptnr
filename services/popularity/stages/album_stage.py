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
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy import text

from db.engine import db_session
from db.utils import row_get

# Enrichment/metadata services
from services.enrichment.album_art_service import (
    save_album_art_to_db,
    fetch_album_art_from_musicbrainz,
)
from services.enrichment.artist_bio_service import get_artist_biography
from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    get_shared_mb_service,
)
from services.catalog.album_classification_service import (
    detect_live_album_type,
    is_live_or_unplugged_track_title,
    normalize_primary_release_type,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Album type detection
# ---------------------------------------------------------------------------

_COMPILATION_ARTISTS = frozenset(["various artists", "various artists -", "various", "compilation", "soundtrack"])
_HETEROGENEOUS_MARKERS = [
    "+compilation", "(compilation)", "+soundtrack", "(soundtrack)",
    "+live", "(live)", "+remix", "(remix)", "+spokenword", "(spokenword)",
]
_LIVE_ALBUM_PATTERNS = [
    r"\blive\s+at\b", r"\blive\s+in\b", r"\blive\s+from\b",
    r"\blive\s+session\b", r"\(live\)\s*$", r"\[live\]\s*$",
    r"-\s*live\s*$", r",\s*live\s*$", r"\+\s*live\s*$",
    r"live\s+recording\b", r"live\s+tour\b", r"\bin\s+concert\b",
    r"\bunplugged\b", r"\bacoustic\b",
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

    if any(re.search(p, album_lower) for p in _LIVE_ALBUM_PATTERNS):
        return "album+live"

    if "+remix" in album_lower or "(remix)" in album_lower:
        return "album+remix"

    return "album"


# ---------------------------------------------------------------------------
# Album art helper
# ---------------------------------------------------------------------------

def _fetch_album_art_with_fallback(artist: str, album: str, discogs_token: str | None = None) -> str | None:
    """Fetch album art: Navidrome first (default), then MusicBrainz/CAA, then AudioDB, then Discogs."""

    # 0) Skip the whole provider chain when art is already in the DB — a
    # repeat/forced scan must not re-hit 4 external services per album
    # (each with 1 req/s throttles + 429 cooldown sleeps) for art that was
    # already fetched in a previous run.  The 360s album-enrichment budget
    # was consistently being eaten by exactly this chain.
    try:
        from db.repositories.metadata import fetch_album_art_blob
        blob, _ = fetch_album_art_blob(artist=artist, album=album)
        if blob:
            return "cached"
    except Exception as exc:
        logger.debug("Album-art cache check failed", artist=artist, album=album, error=str(exc))
    
    # 0) Try Navidrome
    try:
        from services.enrichment.album_art_service import fetch_album_art_from_navidrome
        data = fetch_album_art_from_navidrome(artist, album)
        if data:
            save_album_art_to_db(artist, album, data, source="navidrome")
            return "navidrome"
    except Exception as exc:
        logger.debug("Navidrome art fetch failed", artist=artist, album=album, error=str(exc))

    # 1) Try MusicBrainz / Cover Art Archive
    try:
        data = fetch_album_art_from_musicbrainz(artist, album)
        if data:
            save_album_art_to_db(artist, album, data, source="musicbrainz")
            return "musicbrainz"
    except Exception as exc:
        logger.debug("CAA fetch failed", artist=artist, album=album, error=str(exc))

    # 2) Try AudioDB
    try:
        from api_clients.audiodb import get_album_artwork
        art_url = get_album_artwork(artist, album, enabled=True)
        if art_url:
            resp = httpx.get(art_url, timeout=10)
            if resp.status_code == 200 and resp.content:
                save_album_art_to_db(artist, album, resp.content, source="audiodb")
                return "audiodb"
    except Exception as exc:
        logger.debug("AudioDB art fetch failed", artist=artist, album=album, error=str(exc))

    # 3) Try Discogs
    try:
        from api_clients.discogs_http import DiscogsHttpClient
        if discogs_token and len(discogs_token) >= 10 and discogs_token.lower() not in ("your_discogs_token", "your_token", "placeholder"):
            client = DiscogsHttpClient(discogs_token)
            results = client.search_album_release(artist, album)
            if results and results[0].get("cover_image"):
                resp = httpx.get(results[0]["cover_image"], timeout=10)
                if resp.status_code == 200 and resp.content:
                    save_album_art_to_db(artist, album, resp.content, source="discogs")
                    return "discogs"
    except Exception as exc:
        logger.debug("Discogs art fetch failed", artist=artist, album=album, error=str(exc))

    return None


# ---------------------------------------------------------------------------
# Artist metadata helper
# ---------------------------------------------------------------------------

def _fetch_artist_metadata(artist: str, conn: Any) -> dict[str, Any]:
    """Fetch artist country, bio, and image, delegating to existing services."""
    result: dict[str, Any] = {"country": None, "bio": None, "image_url": None}
    
    with db_session() as session:
        existing = session.execute(
            text("SELECT country, bio, image_url FROM artists WHERE name = :artist"),
            {"artist": artist},
        ).mappings().first()
        if existing:
            result = {
                "country": existing.get("country") or None,
                "bio": existing.get("bio") or None,
                "image_url": existing.get("image_url") or None,
            }

    # Bio via existing enrichment service
    if not result["bio"]:
        try:
            bio = get_artist_biography(artist)
            result["bio"] = str(bio) if bio else None
        except Exception as exc:
            logger.debug("Bio lookup failed", artist=artist, error=str(exc))

    # Country via MusicBrainz Shared Client
    if not result["country"]:
        try:
            mb = get_shared_mb_client()
            country = mb.get_artist_country(artist)
            result["country"] = str(country) if country else None
        except Exception as exc:
            logger.debug("Country lookup failed", artist=artist, error=str(exc))

    # Image via AudioDB
    if not result["image_url"]:
        try:
            from api_clients.audiodb import get_artist_fanart
            img = get_artist_fanart(artist, enabled=True)
            result["image_url"] = str(img) if img else None
        except Exception as exc:
            logger.debug("Image lookup failed", artist=artist, error=str(exc))

    # Persist
    try:
        with db_session() as session:
            session.execute(
                text("""
                    INSERT INTO artists (id, name, country, bio, image_url)
                    VALUES (:artist, :artist, :country, :bio, :image_url)
                    ON CONFLICT (name) DO UPDATE SET
                        country = COALESCE(excluded.country, artists.country),
                        bio = COALESCE(excluded.bio, artists.bio),
                        image_url = COALESCE(excluded.image_url, artists.image_url)
                """),
                {"artist": artist, "country": result["country"], "bio": result["bio"], "image_url": result["image_url"]},
            )
    except Exception as exc:
        logger.debug("Persist artist metadata failed", artist=artist, error=str(exc))

    return result


# ---------------------------------------------------------------------------
# Similar artists helper
# ---------------------------------------------------------------------------

def _fetch_similar_artists(artist: str, conn: Any, options: dict[str, Any]) -> dict[str, list[Any]]:
    """Fetch and cache similar artists from Last.fm and ListenBrainz."""
    result: dict[str, list[Any]] = {"lastfm": [], "listenbrainz": []}

    if bool(options.get("singles_only")) or bool(options.get("singles_with_missing_popularity")):
        return result

    try:
        with db_session() as session:
            cached = session.execute(
                text("""
                    SELECT similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated
                    FROM artists WHERE name = :artist
                """),
                {"artist": artist},
            ).mappings().first()
            
        cached_lf_fresh = False
        cached_lb_fresh = False
        
        if cached:
            lf_raw = row_get(cached, "similar_artists_lastfm")
            lb_raw = row_get(cached, "similar_artists_listenbrainz")
            ts_raw = row_get(cached, "similar_artists_last_updated")
            if lf_raw:
                try:
                    result["lastfm"] = json.loads(lf_raw)
                except Exception:
                    pass
            if lb_raw:
                try:
                    result["listenbrainz"] = json.loads(lb_raw)
                except Exception:
                    pass
            if ts_raw:
                try:
                    ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - ts).days
                    cached_lf_fresh = bool(result["lastfm"]) and age < 90
                    cached_lb_fresh = bool(result["listenbrainz"]) and age < 90
                except Exception:
                    pass

        if cached_lf_fresh and cached_lb_fresh:
            return result

        from helpers.config_helpers import get_config
        cfg = get_config()
        if not cached_lf_fresh:
            lf_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
            if lf_cfg.get("enabled") and lf_cfg.get("api_key"):
                from api_clients.lastfm import LastFmClient
                lf = LastFmClient(lf_cfg["api_key"])
                similar = lf.get_similar_artists(artist, limit=10) or []
                result["lastfm"] = [s.get("name", "") for s in similar if isinstance(s, dict)]

        if not cached_lb_fresh:
            try:
                artist_mbid = None
                with db_session() as session:
                    row = session.execute(
                        text("SELECT NULLIF(TRIM(musicbrainz_artistid), '') AS mbid "
                              "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                              "AND COALESCE(NULLIF(TRIM(musicbrainz_artistid), ''), '') <> '' LIMIT 1"),
                        {"artist": artist},
                    ).fetchone()
                    if row and row_get(row, "mbid"):
                        artist_mbid = row_get(row, "mbid")
                if not artist_mbid:
                    from services.enrichment.musicbrainz_persistence_service import lookup_and_save_artist_mbid
                    artist_mbid = lookup_and_save_artist_mbid(artist, None)
                if artist_mbid:
                    from api_clients.listenbrainz import ListenBrainzClient
                    lb_similar = ListenBrainzClient().get_similar_artists(artist_mbid, limit=10) or []
                    result["listenbrainz"] = [s.get("name", "") for s in lb_similar if isinstance(s, dict) and s.get("name")]
            except Exception as exc:
                logger.debug("ListenBrainz similar artists failed", artist=artist, error=str(exc))

        try:
            with db_session() as session:
                session.execute(
                    text("""
                        INSERT INTO artists (id, name, similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated)
                        VALUES (:artist, :artist, :lf, :lb, :updated)
                        ON CONFLICT (name) DO UPDATE SET
                            similar_artists_lastfm = excluded.similar_artists_lastfm,
                            similar_artists_listenbrainz = excluded.similar_artists_listenbrainz,
                            similar_artists_last_updated = excluded.similar_artists_last_updated
                    """),
                    {
                        "artist": artist,
                        "lf": json.dumps(result["lastfm"]) if result["lastfm"] else None,
                        "lb": json.dumps(result["listenbrainz"]) if result["listenbrainz"] else None,
                        "updated": datetime.now().isoformat(),
                    },
                )
        except Exception as exc:
            logger.debug("Similar-artist persist failed", artist=artist, error=str(exc))
            
    except Exception as exc:
        logger.debug("Similar artists failed entirely", artist=artist, error=str(exc))

    return result


_discogs_artist_id_cache: dict[str, str] = {}
_discogs_artist_id_lock = threading.Lock()

def _fetch_discogs_artist_id(artist: str, conn: Any, options: dict[str, Any]) -> None:
    """Fetch and cache the Discogs artist ID."""
    if bool(options.get("singles_only")) or bool(options.get("singles_with_missing_popularity")):
        return
    if artist.lower() in ("various artists", "various", "compilation", "soundtrack"):
        return
    try:
        from helpers.config_helpers import get_config
        cfg = get_config()
        discogs_cfg = cfg.get("api_integrations", {}).get("discogs", {})
        token = discogs_cfg.get("token", "")
        if not (discogs_cfg.get("enabled") and token):
            return
        if token.lower() in ("your_discogs_token", "your_token", "placeholder", ""):
            return
            
        _cache_key = artist.casefold().strip()

        # Cache check under the lock only — the network call MUST happen
        # outside it.  Holding the module-global lock across a Discogs
        # request (which can sleep up to 60s per 429 cooldown, plus retries)
        # lets an abandoned bounded thread (album enrichment exceeding its
        # budget) deadlock every later caller on ``with _discogs_artist_id_lock``
        # for the rest of the scan — the scan appears frozen with zero logs.
        with _discogs_artist_id_lock:
            discogs_artist_id = _discogs_artist_id_cache.get(_cache_key, "")
        if not discogs_artist_id:
            from api_clients.discogs_http import DiscogsHttpClient
            client = DiscogsHttpClient(token=token)
            discogs_artist_id = str(client.get_artist_id(artist, timeout=12) or "")
            with _discogs_artist_id_lock:
                _discogs_artist_id_cache[_cache_key] = discogs_artist_id

        if not discogs_artist_id:
            return
            
        with db_session() as session:
            session.execute(
                text(
                    "UPDATE tracks SET discogs_artist_id = :did "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND (discogs_artist_id IS NULL OR TRIM(CAST(discogs_artist_id AS TEXT)) = '')"
                ),
                {"did": discogs_artist_id, "artist": artist},
            )
        logger.info("Retrieved and persisted Discogs artist ID", artist=artist, discogs_id=discogs_artist_id)
    except Exception as exc:
        logger.debug("Discogs artist ID lookup failed", artist=artist, error=str(exc))


def _fetch_musicbrainz_artist_id(artist: str, conn: Any, options: dict[str, Any]) -> None:
    """Resolve and persist the MusicBrainz artist ID."""
    if artist.lower() in ("various artists", "various", "compilation", "soundtrack"):
        return
    try:
        with db_session() as session:
            row = session.execute(
                text(
                    "SELECT NULLIF(TRIM(musicbrainz_artistid), '') AS mbid "
                    "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND COALESCE(NULLIF(TRIM(musicbrainz_artistid), ''), '') <> '' LIMIT 1"
                ),
                {"artist": artist},
            ).fetchone()
        if row and row[0]:
            return

        from services.enrichment.musicbrainz_persistence_service import lookup_and_save_artist_mbid
        mbid = lookup_and_save_artist_mbid(artist)
        if mbid:
            logger.info("Persisted MusicBrainz artist ID", artist=artist, mbid=mbid)
    except Exception as exc:
        logger.debug("MusicBrainz artist ID lookup failed", artist=artist, error=str(exc))


def _lookup_musicbrainz_album_type(artist: str, album: str) -> tuple[str | None, str | None]:
    """Query MusicBrainz release-group for a confident album-type match."""
    try:
        svc = get_shared_mb_service()
        matches = svc.search_releasegroup_matches(artist, album, limit=3)
        if not matches:
            return None, None
        best = matches[0]
        if (best.get("match_score") or 0) < 0.6:
            return None, None
        primary = (best.get("primary_type") or "").lower()
        rg_mbid = best.get("id")
        
        secondary = " ".join(str(s).lower() for s in (best.get("secondary_types") or []) if s)
        mapping = {
            "single": "single",
            "ep": "ep",
            "album": "album",
            "compilation": "album+compilation",
            "live": "album+live",
            "remix": "album+remix",
        }
        if primary == "album" or primary not in mapping:
            if "live" in secondary:
                return "album+live", rg_mbid
            if "acoustic" in secondary or "unplugged" in secondary:
                return "album+acoustic", rg_mbid
            if "compilation" in secondary:
                return "album+compilation", rg_mbid
            if "remix" in secondary:
                return "album+remix", rg_mbid
        return mapping.get(primary), rg_mbid
    except Exception as exc:
        logger.debug("MB album-type lookup failed", artist=artist, album=album, error=str(exc))
        return None, None


def _persist_album_type_to_tracks(conn: Any, cursor: Any, artist: str, album: str, tracks: list[dict[str, Any]], album_type: str, release_group_mbid: str | None) -> None:
    """Propagate the detected album type to album tracks."""
    if not album_type:
        return
        
    primary = normalize_primary_release_type(album_type)
    updated = 0
    with db_session() as session:
        for track in tracks or []:
            track_id = track.get("id")
            if not track_id:
                continue
            current_type = str(track.get("musicbrainz_albumtype") or "")
            if current_type == album_type:
                continue
            try:
                session.execute(
                    text("""
                        UPDATE tracks
                        SET spotify_album_type = :album_type, releasetype = :primary, musicbrainz_albumtype = :album_type
                        WHERE id = :tid
                    """),
                    {"album_type": album_type, "primary": primary, "tid": str(track_id)},
                )
                updated += 1
            except Exception as exc:
                logger.debug("Album-type persist failed", track_id=track_id, error=str(exc))
                
    if updated:
        logger.info("Persisted album type", album_type=album_type, tracks_updated=updated, artist=artist, album=album)

    if release_group_mbid:
        with db_session() as session:
            try:
                session.execute(
                    text("""
                        UPDATE tracks
                        SET musicbrainz_releasegroupid = :rg_mbid
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                          AND (musicbrainz_releasegroupid IS NULL OR TRIM(musicbrainz_releasegroupid) = '')
                    """),
                    {"rg_mbid": release_group_mbid, "artist": artist, "album": album},
                )
            except Exception as exc:
                logger.debug("Release-group MBID propagation failed", error=str(exc))

        release_mbid = ""
        try:
            from services.enrichment.musicbrainz_service import resolve_release_id
            release_mbid = resolve_release_id(release_group_mbid)
        except Exception as exc:
            logger.debug("Release MBID resolution failed", release_group_mbid=release_group_mbid, error=str(exc))
            release_mbid = ""
            
        if release_mbid and str(release_mbid).strip() and str(release_mbid).strip() != str(release_group_mbid).strip():
            try:
                with db_session() as session:
                    session.execute(
                        text("""
                            UPDATE tracks
                            SET musicbrainz_album_mbid = :mbid, musicbrainz_albumid = :mbid
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                              AND (musicbrainz_album_mbid IS NULL OR TRIM(musicbrainz_album_mbid) = '')
                        """),
                        {"mbid": release_mbid, "artist": artist, "album": album},
                    )
            except Exception as exc:
                logger.debug("Release MBID propagation failed", error=str(exc))


def _inject_album_genre(track_id: str, label: str, mb_genres_raw: Any, genres_raw: Any) -> None:
    """Insert a genre label (Live/Acoustic/Remix) into stored genre columns."""
    mb_list: list[str] = []
    try:
        if mb_genres_raw and str(mb_genres_raw) not in ("null", "[]", ""):
            mb_list = json.loads(mb_genres_raw) if isinstance(mb_genres_raw, str) else list(mb_genres_raw)
    except Exception:
        mb_list = []
        
    if not isinstance(mb_list, list):
        mb_list = []
        
    if label.lower() not in [str(g).lower() for g in mb_list]:
        mb_list.insert(0, label)

    genre_list = [g.strip() for g in str(genres_raw or "").split(",") if g.strip()]
    if label.lower() not in [g.lower() for g in genre_list]:
        genre_list.insert(0, label)

    try:
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET musicbrainz_genres = :mb, genres = :genres WHERE id = :tid"),
                {"mb": json.dumps(mb_list), "genres": ", ".join(genre_list), "tid": str(track_id)},
            )
    except Exception as exc:
        logger.debug("Genre label inject failed", track_id=track_id, error=str(exc))


def _fetch_artist_lastfm_tags(artist: str, conn: Any) -> None:
    """Fetch and cache Last.fm artist top tags."""
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT lastfm_artist_tags FROM artists WHERE name = :artist"),
                {"artist": artist},
            ).fetchone()
            if row and row[0]:
                return

        from helpers.config_helpers import get_config
        cfg = get_config()
        lf_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
        if not (lf_cfg.get("enabled") and lf_cfg.get("api_key")):
            return
        api_key = lf_cfg["api_key"]
        if api_key in ("your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>", ""):
            return

        from api_clients.lastfm import LastFmClient
        lf = LastFmClient(api_key)
        tags = lf.get_artist_top_tags(artist, limit=15) or []
        names = [t.get("name") for t in tags if isinstance(t, dict) and t.get("name")]
        if names:
            with db_session() as session:
                session.execute(
                    text("UPDATE artists SET lastfm_artist_tags = :tags WHERE name = :artist"),
                    {"tags": json.dumps(names), "artist": artist},
                )
            logger.info("Stored Last.fm tag(s)", tag_count=len(names), artist=artist)
    except Exception as exc:
        logger.debug("Last.fm artist tags failed", artist=artist, error=str(exc))


def ensure_album_type(album_row: dict[str, Any], options: dict[str, Any] | None = None) -> str | None:
    """Lightweight album-type enrichment for SKIPPED albums."""
    options = options or {}
    artist = str(album_row.get("artist") or "")
    album = str(album_row.get("album") or "")
    tracks = album_row.get("tracks") or []
    if not artist or not album:
        return None

    stored = {
        str(t.get("musicbrainz_albumtype") or "").strip()
        for t in tracks
        if t.get("id")
    }
    stored.discard("")
    if len(stored) == 1 and not options.get("force"):
        return next(iter(stored))

    detected: str | None = None
    try:
        detected = _detect_album_type(
            artist,
            album,
            str(album_row.get("album_artist") or "") or None,
            str(album_row.get("spotify_album_type") or "") or None,
        )
        mb_type, rg_mbid = _lookup_musicbrainz_album_type(artist, album)
        if mb_type:
            track_count = len(tracks)
            if mb_type in ("single", "ep") and track_count > 6:
                mb_type = "album"
            elif mb_type == "single" and track_count > 3:
                mb_type = "ep"
            if detected == "album" or mb_type in ("single", "ep"):
                detected = mb_type
        if not detected:
            return None
        _persist_album_type_to_tracks(None, None, artist, album, tracks, detected, rg_mbid)
        logger.info("Ensured album type", detected=detected, artist=artist, album=album)
        return detected
    except Exception as exc:
        logger.debug("ensure_album_type failed", artist=artist, album=album, error=str(exc))
        return detected


def _apply_live_remix_album_tagging(artist: str, album: str, album_type: str, tracks: list[dict[str, Any]]) -> None:
    """Tag live/acoustic/remix albums."""
    lower = (album_type or "").lower()
    is_live_album = "+live" in lower or "(live)" in lower
    is_remix_album = "+remix" in lower or "(remix)" in lower

    if is_live_album:
        live_type = detect_live_album_type(album, album_type) or "live"
        label = "Acoustic" if live_type == "acoustic" else "Live"
        tagged = 0
        with db_session() as session:
            for track in tracks or []:
                track_id = track.get("id")
                title = str(track.get("title") or "")
                if not track_id or not title:
                    continue
                is_live_flag = int(track.get("is_live") or 0)
                is_acoustic_flag = int(track.get("is_acoustic") or 0)
                already_tagged = (
                    (label == "Live" and is_live_flag)
                    or (label == "Acoustic" and is_acoustic_flag)
                )
                if already_tagged:
                    continue
                new_title = title
                has_suffix = bool(re.search(rf"\({label}[^)]*\)\s*$", title, re.IGNORECASE))
                if not is_live_or_unplugged_track_title(title) and not has_suffix:
                    new_title = f"{title} ({label})"
                try:
                    session.execute(
                        text("""
                            UPDATE tracks
                            SET is_live = :is_live, is_acoustic = :is_acoustic,
                                album_context_live = 1, title = :title
                            WHERE id = :tid
                        """),
                        {
                            "is_live": 1 if label == "Live" else 0,
                            "is_acoustic": 1 if label == "Acoustic" else 0,
                            "title": new_title,
                            "tid": str(track_id),
                        },
                    )
                    tagged += 1
                    _inject_album_genre(str(track_id), label, track.get("musicbrainz_genres"), track.get("genres"))
                except Exception as exc:
                    logger.debug("Live tagging failed", track_id=track_id, error=str(exc))
        if tagged:
            logger.info("Tagged as Live/Acoustic album", artist=artist, album=album, label=label)

    if is_remix_album:
        tagged = 0
        with db_session() as session:
            for track in tracks or []:
                track_id = track.get("id")
                if not track_id:
                    continue
                try:
                    session.execute(
                        text("UPDATE tracks SET is_remix = 1 WHERE id = :tid AND COALESCE(is_remix, 0) = 0"),
                        {"tid": str(track_id)},
                    )
                    tagged += 1
                    _inject_album_genre(str(track_id), "Remix", track.get("musicbrainz_genres"), track.get("genres"))
                except Exception as exc:
                    logger.debug("Remix tagging failed", track_id=track_id, error=str(exc))
        if tagged:
            logger.info("Tagged as Remix album", artist=artist, album=album)


_LIVE_SUFFIX_RE = re.compile(r"\s*\((?:Live|Acoustic)[^)]*\)\s*$", re.IGNORECASE)


def strip_live_acoustic_suffix(title: str) -> str:
    return _LIVE_SUFFIX_RE.sub("", title or "").strip()


def _drop_live_genres_from_json(raw: Any) -> str | None:
    try:
        if not raw:
            return None
        parsed = json.loads(raw) if isinstance(raw, str) else list(raw)
        kept = [g for g in parsed if str(g).strip().lower() not in ("live", "acoustic")]
        return json.dumps(kept) if kept != parsed else None
    except Exception:
        return None


def _drop_live_genres_from_csv(raw: Any) -> str | None:
    try:
        if not raw:
            return None
        parts = [g.strip() for g in str(raw).split(",") if g.strip()]
        kept = [g for g in parts if g.lower() not in ("live", "acoustic")]
        return ", ".join(kept) if kept != parts else None
    except Exception:
        return None


def revert_track_live_state(track_id: str) -> bool:
    """Undo album-stage live/acoustic tagging for a single track."""
    try:
        import os as _os
        with db_session() as session:
            row = session.execute(
                text("SELECT title, file_path, genres, musicbrainz_genres "
                     "FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
            if not row:
                return False
            r = row._mapping
            old_title = str(r.get("title") or "")
            new_title = strip_live_acoustic_suffix(old_title)
            new_mb = _drop_live_genres_from_json(r.get("musicbrainz_genres"))
            new_genres = _drop_live_genres_from_csv(r.get("genres"))
            session.execute(
                text("""
                    UPDATE tracks
                    SET title = :title, is_live = 0, is_acoustic = 0,
                        album_context_live = 0,
                        musicbrainz_genres = COALESCE(:mb, musicbrainz_genres),
                        genres = COALESCE(:genres, genres)
                    WHERE CAST(id AS TEXT) = :id
                """),
                {"id": str(track_id), "title": new_title or old_title,
                 "mb": new_mb, "genres": new_genres},
            )
            file_path = r.get("file_path")

        if new_title != old_title and file_path:
            try:
                from services.metadata.tag_file_service import update_file_tags
                resolved = str(file_path)
                if not _os.path.isabs(resolved):
                    from helpers.config_helpers import get_config
                    music_root = (get_config().get("music", {}) or {}).get("root") or _os.environ.get("MUSIC_ROOT", "/music")
                    resolved = _os.path.join(music_root, resolved)
                if _os.path.exists(resolved):
                    tags: dict[str, Any] = {"title": new_title or old_title}
                    if new_genres is not None:
                        tags["genres"] = [g.strip() for g in new_genres.split(",") if g.strip()]
                    update_file_tags(resolved, tags)
            except Exception as tag_err:
                logger.debug("Live revert tag write failed", track_id=track_id, error=str(tag_err))

        logger.info("Reverted live/acoustic tagging for track", track_id=track_id, old_title=old_title, new_title=new_title or old_title)
        return True
    except Exception as exc:
        logger.debug("Live-state revert failed", track_id=track_id, error=str(exc))
        return False


def _persist_alternate_takes(album_context: dict[str, Any]) -> None:
    """Mark alternate takes (``alternate_take`` / ``base_track_id``)."""
    alternate_takes = (album_context or {}).get("alternate_takes") or {}
    updated = 0
    with db_session() as session:
        for base_key, variants in alternate_takes.items():
            if not variants or len(variants) < 2:
                continue
            base_track = variants[0]
            base_id = base_track.get("id") if isinstance(base_track, dict) else None
            if not base_id:
                continue
            for variant in variants[1:]:
                alt_id = variant.get("id") if isinstance(variant, dict) else None
                if alt_id and str(alt_id) != str(base_id):
                    try:
                        session.execute(
                            text("UPDATE tracks SET alternate_take = 1, base_track_id = :base_id "
                                  "WHERE id = :alt_id AND COALESCE(alternate_take, 0) = 0"),
                            {"base_id": str(base_id), "alt_id": str(alt_id)},
                        )
                        updated += 1
                    except Exception as exc:
                        logger.debug("Alternate-take persist failed", alt_id=alt_id, error=str(exc))
    if updated:
        logger.info("Marked alternate take(s) in album", count=updated)


def _get_discogs_token() -> str | None:
    try:
        from helpers.config_helpers import get_config
        cfg = get_config()
        token = cfg.get("api_integrations", {}).get("discogs", {}).get("token")
        if token and str(token).lower() in ("your_discogs_token", "your_token", "placeholder", ""):
            return None
        return token
    except Exception:
        return None


def _run_full_enrichment(
    artist: str,
    album: str,
    album_context: dict[str, Any],
    album_tracks: list[dict[str, Any]],
    detected_type: str,
    options: dict[str, Any],
    discogs_token: str | None,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Album art, artist metadata, tags, similar artists, live/remix tagging.

    Each step logs its duration at DEBUG so enabling debug in config.html
    surfaces exactly where enrichment time goes (album art chain, artist
    metadata, similar artists, etc.) in unified_scan.log.
    """
    _enrich_start = time.monotonic()

    _step_start = time.monotonic()
    logger.info(
        "[ENRICH] ▶ album art lookup (Navidrome → MB/CAA → AudioDB → Discogs)",
        artist=artist, album=album,
    )
    art_source = _fetch_album_art_with_fallback(artist, album, discogs_token)
    if art_source:
        logger.info("Album art cached", artist=artist, album=album, source=art_source)
    logger.info(
        "[ENRICH] ✓ album art lookup done",
        artist=artist, album=album, source=art_source,
        elapsed_s=round(time.monotonic() - _step_start, 1),
    )

    _step_start = time.monotonic()
    logger.info(
        "[ENRICH] ▶ artist metadata (bio / country / image)",
        artist=artist,
    )
    meta = _fetch_artist_metadata(artist, None)
    logger.info(
        "[ENRICH] ✓ artist metadata done",
        artist=artist,
        country=meta.get("country"), has_bio=bool(meta.get("bio")), has_image=bool(meta.get("image_url")),
        elapsed_s=round(time.monotonic() - _step_start, 1),
    )

    _step_start = time.monotonic()
    logger.info(
        "[ENRICH] ▶ artist lastfm tags",
        artist=artist,
    )
    _fetch_artist_lastfm_tags(artist, None)
    logger.info(
        "[ENRICH] ✓ artist lastfm tags done",
        artist=artist,
        elapsed_s=round(time.monotonic() - _step_start, 1),
    )

    if meta.get("country"):
        try:
            with db_session() as session:
                session.execute(
                    text("UPDATE tracks SET releasecountry = :country "
                          "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                          "AND (releasecountry IS NULL OR TRIM(releasecountry) = '')"),
                    {"country": meta["country"], "artist": artist},
                )
        except Exception as exc:
            logger.debug("releasecountry backfill failed", error=str(exc))

    _step_start = time.monotonic()
    logger.info(
        "[ENRICH] ▶ artist musicbrainz id",
        artist=artist,
    )
    _fetch_musicbrainz_artist_id(artist, None, options)
    logger.info(
        "[ENRICH] ✓ artist musicbrainz id done",
        artist=artist,
        elapsed_s=round(time.monotonic() - _step_start, 1),
    )

    _step_start = time.monotonic()
    logger.info(
        "[ENRICH] ▶ similar artists (Last.fm + ListenBrainz)",
        artist=artist,
    )
    similar = _fetch_similar_artists(artist, None, options)
    logger.info(
        "[ENRICH] ✓ similar artists done",
        artist=artist,
        lastfm_count=len(similar.get("lastfm") or []),
        listenbrainz_count=len(similar.get("listenbrainz") or []),
        elapsed_s=round(time.monotonic() - _step_start, 1),
    )

    _step_start = time.monotonic()
    logger.info(
        "[ENRICH] ▶ artist discogs id",
        artist=artist,
    )
    _fetch_discogs_artist_id(artist, None, options)
    logger.info(
        "[ENRICH] ✓ artist discogs id done",
        artist=artist,
        elapsed_s=round(time.monotonic() - _step_start, 1),
    )

    _apply_live_remix_album_tagging(artist, album, detected_type, album_tracks)
    _persist_alternate_takes(album_context)

    logger.info(
        "[ENRICH] ✓ album enrichment complete",
        artist=artist, album=album,
        total_s=round(time.monotonic() - _enrich_start, 1),
    )

    return meta, similar


def enrich_album_extras(
    *,
    artist: str,
    album: str,
    album_context: dict[str, Any],
    album_tracks: list[dict[str, Any]],
    detected_type: str,
    options: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[Any]], dict[str, Any]]:
    """Post-singles full metadata import for full scans."""
    meta, similar = _run_full_enrichment(
        artist,
        album,
        album_context,
        album_tracks,
        detected_type,
        options,
        _get_discogs_token(),
    )
    extra_context: dict[str, Any] = {}
    if meta.get("country"):
        extra_context["artist_country"] = meta["country"]
    if similar["lastfm"]:
        extra_context["similar_artists_lastfm"] = similar["lastfm"]
    if similar["listenbrainz"]:
        extra_context["similar_artists_listenbrainz"] = similar["listenbrainz"]
    return extra_context, similar, meta


def enrich_album(
    *,
    album_row: dict[str, Any],
    album_context: dict[str, Any],
    stat_eligible_tracks: list[dict[str, Any]],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Run album-level enrichment."""
    artist = str(album_row.get("artist") or "")
    album = str(album_row.get("album") or "")
    album_artist = str(album_row.get("album_artist") or "")
    spotify_type = str(
        album_row.get("musicbrainz_album_type")
        or album_row.get("spotify_album_type")
        or ""
    )

    _singles_pass = bool(
        options.get("singles_only")
        or options.get("singles_with_missing_popularity")
        or options.get("singles_detection_only")
    )
    _popularity_pass = bool(options.get("popularity_only"))
    _meta: dict[str, Any] = {"country": None, "bio": None, "image_url": None}
    _similar: dict[str, list[Any]] = {"lastfm": [], "listenbrainz": []}

    discogs_token = _get_discogs_token()
    album_tracks = album_row.get("tracks") or []
    
    try:
        _detect_start = time.monotonic()
        logger.debug(
            "[ENRICH] ▶ album type detection",
            artist=artist, album=album,
        )
        detected_type = _detect_album_type(artist, album, album_artist or None, spotify_type or None)
        if _popularity_pass:
            is_hetero = any(m in detected_type.lower() for m in _HETEROGENEOUS_MARKERS)
        else:
            mb_type, rg_mbid = _lookup_musicbrainz_album_type(artist, album)
            if mb_type:
                track_count = len(album_tracks)
                if mb_type in ("single", "ep") and track_count > 6:
                    mb_type = "album"
                elif mb_type == "single" and track_count > 3:
                    mb_type = "ep"
                if detected_type == "album" or mb_type in ("single", "ep"):
                    detected_type = mb_type
                    logger.info("MB album type detected", artist=artist, album=album, type=detected_type)
                    
            is_hetero = any(m in detected_type.lower() for m in _HETEROGENEOUS_MARKERS)
            logger.info("Album type resolved", artist=artist, album=album, type=detected_type, heterogeneous=is_hetero)
            logger.debug(
                "[ENRICH] ✓ album type detection done",
                artist=artist, album=album,
                detected_type=detected_type,
                mb_type=mb_type,
                elapsed_s=round(time.monotonic() - _detect_start, 1),
            )

            _persist_start = time.monotonic()
            logger.debug(
                "[ENRICH] ▶ persist album type + release resolution (resolve_release_id)",
                artist=artist, album=album,
            )
            _persist_album_type_to_tracks(None, None, artist, album, album_tracks, detected_type, rg_mbid)
            logger.debug(
                "[ENRICH] ✓ persist album type done",
                artist=artist, album=album,
                elapsed_s=round(time.monotonic() - _persist_start, 1),
            )

            if "+compilation" in detected_type.lower() or "+soundtrack" in detected_type.lower():
                try:
                    with db_session() as session:
                        session.execute(
                            text("UPDATE tracks SET is_compilation = 1 "
                                  "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album "
                                  "AND COALESCE(is_compilation, 0) = 0"),
                            {"artist": artist, "album": album},
                        )
                except Exception as exc:
                    logger.debug("Compilation flag persist failed", error=str(exc))

        if _popularity_pass or _singles_pass:
            art_source = None
            meta = _meta
            similar = _similar
            if _singles_pass:
                _fetch_musicbrainz_artist_id(artist, None, options)
        else:
            if bool(options.get("defer_full_enrichment")):
                art_source = None
                meta = _meta
                similar = _similar
                _fetch_musicbrainz_artist_id(artist, None, options)
            else:
                meta, similar = _run_full_enrichment(
                    artist, album, album_context, album_tracks,
                    detected_type, options, discogs_token,
                )

    except Exception as exc:
        logger.error("Enrichment failed", artist=artist, album=album, error=str(exc), exc_info=True)
        detected_type = "album"
        is_hetero = False
        art_source = None
        meta = {"country": None, "bio": None, "image_url": None}
        similar = {"lastfm": [], "listenbrainz": []}

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
