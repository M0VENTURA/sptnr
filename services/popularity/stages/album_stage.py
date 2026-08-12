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
import re
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
from services.catalog.album_classification_service import (
    detect_live_album_type,
    is_live_or_unplugged_track_title,
    normalize_primary_release_type,
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
    """Fetch album art: Navidrome first (default), then MusicBrainz/CAA, then AudioDB, then Discogs.

    Navidrome already holds the art the user sees in their library, so it is
    the preferred source; external providers are only consulted when it has
    no art.
    """
    # 0) Try Navidrome (default source — no external calls needed)
    try:
        from services.enrichment.album_art_service import (
            fetch_album_art_from_navidrome,
            save_album_art_to_db,
        )

        data = fetch_album_art_from_navidrome(artist, album)
        if data:
            save_album_art_to_db(artist, album, data, source="navidrome")
            return "navidrome"
    except Exception as exc:
        logger.debug("[album_stage] Navidrome art failed: %s", exc)

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
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    with _db_session() as session:
        existing = session.execute(
            _text("SELECT country, bio, image_url FROM artists WHERE name = :artist"),
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
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            session.execute(
                _text("""
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
        logger.debug("[album_stage] Persist artist metadata failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Similar artists helper (no existing service — kept here for now)
# ---------------------------------------------------------------------------

def _fetch_similar_artists(artist: str, conn, options: dict) -> dict[str, list]:
    """Fetch and cache similar artists from Last.fm (and ListenBrainz in future)."""
    result: dict[str, list] = {"lastfm": [], "listenbrainz": []}
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    if bool(options.get("singles_only")) or bool(options.get("singles_with_missing_popularity")):
        return result

    try:
        with _db_session() as session:
            cached = session.execute(
                _text("""
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
                    result["lastfm"] = []
            if lb_raw:
                try:
                    result["listenbrainz"] = json.loads(lb_raw)
                except Exception:
                    result["listenbrainz"] = []
            if ts_raw:
                try:
                    ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - ts).days
                    # Per-source freshness: a source is served from cache only
                    # when it has data AND is under the 90-day TTL.  An empty
                    # ListenBrainz result must NEVER block a retry — the old
                    # whole-row gate returned on ANY fresh cached source, so a
                    # first scan that cached only Last.fm left ListenBrainz
                    # empty for 90 days.
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

        # ListenBrainz similar artists (labs API) — requires the artist MBID.
        # Retried on every scan until it yields data (or the cache holds a
        # fresh non-empty result).
        if not cached_lb_fresh:
            try:
                artist_mbid = None
                cursor.execute(
                    "SELECT NULLIF(TRIM(musicbrainz_artistid), '') AS mbid "
                    "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s "
                    "AND COALESCE(NULLIF(TRIM(musicbrainz_artistid), ''), '') <> '' LIMIT 1",
                    (artist,),
                )
                row = cursor.fetchone()
                if row and row_get(row, "mbid"):
                    artist_mbid = row_get(row, "mbid")
                if not artist_mbid:
                    # Proper artist search (NOT a recording search — the
                    # previous get_suggested_mbid(artist, "") call searched
                    # for a recording titled after the artist and returned a
                    # recording MBID, which the LB similar-artists API
                    # rejected).
                    from services.enrichment.musicbrainz_persistence_service import lookup_and_save_artist_mbid
                    artist_mbid = lookup_and_save_artist_mbid(artist, conn)
                if artist_mbid:
                    from api_clients.listenbrainz import ListenBrainzClient
                    lb_similar = ListenBrainzClient().get_similar_artists(artist_mbid, limit=10) or []
                    result["listenbrainz"] = [s.get("name", "") for s in lb_similar if isinstance(s, dict) and s.get("name")]
            except Exception as exc:
                logger.debug("[album_stage] ListenBrainz similar artists failed for '%s': %s", artist, exc)

        cursor.execute("""
            INSERT INTO artists (id, name, similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                similar_artists_lastfm = excluded.similar_artists_lastfm,
                similar_artists_listenbrainz = excluded.similar_artists_listenbrainz,
                similar_artists_last_updated = excluded.similar_artists_last_updated
        """, (artist, artist, json.dumps(result["lastfm"]) if result["lastfm"] else None,
              json.dumps(result["listenbrainz"]) if result["listenbrainz"] else None, datetime.now().isoformat()))
        conn.commit()
    except Exception as exc:
        logger.debug("[album_stage] Similar artists failed: %s", exc)

    return result


# Per-process Discogs artist-ID cache: artist.casefold() → id ("" when the
# artist was looked up and not found).  ``_fetch_discogs_artist_id`` runs once
# per ALBUM — the API call was previously unconditional (only the DB UPDATE
# was guarded), so a 10-album artist paid 10 search_database calls for one
# id.  The cache turns that into one call per artist per process.
_discogs_artist_id_cache: dict[str, str] = {}


def _fetch_discogs_artist_id(artist: str, conn, options: dict) -> None:
    """Fetch and cache the Discogs artist ID (legacy parity).

    Skips compilation groups and singles/metadata-only scan modes, matching
    the legacy scanner. Stores the ID on all of the artist's tracks.
    """
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
        if _cache_key not in _discogs_artist_id_cache:
            from api_clients.discogs import DiscogsClient
            client = DiscogsClient(token=token)
            # One API call per artist per process (was once per album).
            discogs_artist_id = str(client.get_artist_id(artist, timeout=12) or "")
            _discogs_artist_id_cache[_cache_key] = discogs_artist_id
        discogs_artist_id = _discogs_artist_id_cache[_cache_key]
        if not discogs_artist_id:
            return
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            session.execute(
                _text(
                    "UPDATE tracks SET discogs_artist_id = :did "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist "
                    "AND (discogs_artist_id IS NULL OR TRIM(CAST(discogs_artist_id AS TEXT)) = '')"
                ),
                {"did": discogs_artist_id, "artist": artist},
            )
        logger.info("[album_stage] Discogs artist ID for '%s': %s", artist, discogs_artist_id)
    except Exception as exc:
        logger.debug("[album_stage] Discogs artist ID lookup failed for '%s': %s", artist, exc)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def _fetch_musicbrainz_artist_id(artist: str, conn, options: dict) -> None:
    """Resolve and persist the MusicBrainz artist ID (legacy parity).

    Mirrors ``_fetch_discogs_artist_id``: skips compilation groups, resolves
    the ID via a scored MusicBrainz artist search when the artist's tracks
    carry none, and stores it on all of the artist's tracks. The artist page
    ID field, ListenBrainz similar artists, missing-releases detection and
    single detection all depend on this value — the per-recording lookup in
    track_stage alone never populates it for tracks that already have a
    recording MBID (the metadata lookup is skipped for those).
    """
    if artist.lower() in ("various artists", "various", "compilation", "soundtrack"):
        return
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            row = session.execute(
                _text(
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
            logger.info("[album_stage] MusicBrainz artist ID for '%s': %s", artist, mbid)
    except Exception as exc:
        logger.debug("[album_stage] MusicBrainz artist ID lookup failed for '%s': %s", artist, exc)


def _lookup_musicbrainz_album_type(artist: str, album: str) -> tuple[str | None, str | None]:
    """Query MusicBrainz release-group for a confident album-type match.

    Returns ``(album_type, release_group_mbid)``. ``album_type`` is one of
    ``single``, ``ep``, ``album``, ``album+compilation``, ``album+live``,
    ``album+remix`` — or ``None`` when no confident match exists. This
    restores the legacy ``get_album_type_with_fallback`` MusicBrainz path that
    the staged runner had dropped in favour of name-only detection.
    """
    try:
        from services.enrichment.musicbrainz_service import MusicBrainzService
        svc = MusicBrainzService(enabled=True)
        matches = svc.search_releasegroup_matches(artist, album, limit=3)
        if not matches:
            return None, None
        best = matches[0]
        if (best.get("match_score") or 0) < 0.6:
            return None, None
        primary = (best.get("primary_type") or "").lower()
        rg_mbid = best.get("id")
        # Secondary types refine the primary type: a release-group whose
        # primary type is "album" but is tagged secondary "live" is a LIVE
        # album (and "compilation"/"remix" refine to those verdicts).  This
        # keeps the album TYPE the authoritative live signal instead of
        # falling back to title text.
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
        logger.debug("[album_stage] MB album-type lookup failed for '%s - %s': %s", artist, album, exc)
        return None, None


def _persist_album_type_to_tracks(conn, cursor, artist, album, tracks, album_type, release_group_mbid):
    """Propagate the detected album type (+ release-group MBID) to album tracks."""
    if not album_type:
        return
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    primary = normalize_primary_release_type(album_type)
    updated = 0
    with _db_session() as session:
        for track in tracks or []:
            track_id = track.get("id")
            if not track_id:
                continue
            # Skip tracks whose stored type already matches — avoids a write
            # storm on every re-scan (legacy behaviour wrote only on change).
            # Compare against musicbrainz_albumtype — the canonical display
            # column (spotify_album_type is legacy and no longer read).
            current_type = str(track.get("musicbrainz_albumtype") or "")
            if current_type == album_type:
                continue
            try:
                session.execute(
                    _text("""
                        UPDATE tracks
                        SET spotify_album_type = :album_type, releasetype = :primary, musicbrainz_albumtype = :album_type
                        WHERE id = :tid
                    """),
                    {"album_type": album_type, "primary": primary, "tid": str(track_id)},
                )
                updated += 1
            except Exception as exc:
                logger.debug("[album_stage] Album-type persist failed for %s: %s", track_id, exc)
    if updated:
        logger.info(
            "[album_stage] Persisted album type '%s' to %s track(s) for '%s - %s'",
            album_type, updated, artist, album,
        )

    if release_group_mbid:
        with _db_session() as session:
            try:
                session.execute(
                    _text("""
                        UPDATE tracks
                        SET musicbrainz_releasegroupid = :rg_mbid
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                          AND (musicbrainz_releasegroupid IS NULL OR TRIM(musicbrainz_releasegroupid) = '')
                    """),
                    {"rg_mbid": release_group_mbid, "artist": artist, "album": album},
                )
            except Exception as exc:
                logger.debug("[album_stage] Release-group MBID propagation failed: %s", exc)

        # Resolve the release-group MBID to a concrete release MBID and store
        # it on tracks that are still missing one (legacy parity). The album
        # page's "MusicBrainz Release ID" field reads musicbrainz_album_mbid,
        # and Navidrome groups tracks by release MBID — without this, albums
        # whose audio files carry no release tag stay ungrouped.
        # NOTE: never store a release-group MBID in the release-level column —
        # when resolution fails, resolve_release_id returns the input
        # unchanged, and we skip the write entirely.
        release_mbid = ""
        try:
            from services.enrichment.musicbrainz_service import resolve_release_id
            release_mbid = resolve_release_id(release_group_mbid)
        except Exception as exc:
            logger.debug("[album_stage] Release MBID resolution failed for %s: %s", release_group_mbid, exc)
            release_mbid = ""
        if release_mbid and str(release_mbid).strip() and str(release_mbid).strip() != str(release_group_mbid).strip():
            try:
                with _db_session() as session:
                    session.execute(
                        _text("""
                            UPDATE tracks
                            SET musicbrainz_album_mbid = :mbid, musicbrainz_albumid = :mbid
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                              AND (musicbrainz_album_mbid IS NULL OR TRIM(musicbrainz_album_mbid) = '')
                        """),
                        {"mbid": release_mbid, "artist": artist, "album": album},
                    )
            except Exception as exc:
                logger.debug("[album_stage] Release MBID propagation failed: %s", exc)


def _inject_album_genre(conn, cursor, track_id: str, label: str, mb_genres_raw, genres_raw) -> None:
    """Insert a genre label (Live/Acoustic/Remix) into stored genre columns."""
    mb_list: list = []
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

    cursor.execute(
        "UPDATE tracks SET musicbrainz_genres = %s, genres = %s WHERE id = %s",
        (json.dumps(mb_list), ", ".join(genre_list), str(track_id)),
    )


def _fetch_artist_lastfm_tags(artist: str, conn) -> None:
    """Fetch and cache Last.fm artist top tags (legacy parity).

    Stores a JSON list of tag names in ``artists.lastfm_artist_tags``.
    Skips artists that already have tags cached.
    """
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            row = session.execute(
                _text("SELECT lastfm_artist_tags FROM artists WHERE name = :artist"),
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
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            with _db_session() as session:
                session.execute(
                    _text("UPDATE artists SET lastfm_artist_tags = :tags WHERE name = :artist"),
                    {"tags": json.dumps(names), "artist": artist},
                )
            logger.info("[album_stage] Stored %d Last.fm tag(s) for '%s'", len(names), artist)
    except Exception as exc:
        logger.debug("[album_stage] Last.fm artist tags failed for '%s': %s", artist, exc)


def ensure_album_type(album_row: dict[str, Any], options: dict[str, Any] | None = None) -> str | None:
    """Lightweight album-type enrichment for SKIPPED albums.

    Runs when the popularity diff check skips an album but it still needs its
    album type (re)set during a combined scan. Reuses a consistent stored
    verdict when one exists (zero API cost); only albums missing a type hit
    the MusicBrainz release-group lookup. Forced scans always re-verify.

    Returns the detected album type (e.g. ``"album"``, ``"album+compilation"``,
    ``"single"``, ``"ep"``) or ``None`` when no type could be determined.
    """
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
        return next(iter(stored))  # tracks already carry a consistent verdict — reuse it

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
            # Same single/EP downgrade guards as the full enrich path.
            if mb_type in ("single", "ep") and track_count > 6:
                mb_type = "album"
            elif mb_type == "single" and track_count > 3:
                mb_type = "ep"
            if detected == "album" or mb_type in ("single", "ep"):
                detected = mb_type
        if not detected:
            return None
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            _persist_album_type_to_tracks(conn, cursor, artist, album, tracks, detected, rg_mbid)
        finally:
            conn.close()
        logger.info("[album_stage] Ensured album type '%s' for skipped '%s - %s'", detected, artist, album)
        return detected
    except Exception as exc:
        logger.debug("[album_stage] ensure_album_type failed for '%s - %s': %s", artist, album, exc)
        return detected


def _apply_live_remix_album_tagging(conn, cursor, artist, album, album_type, tracks) -> None:
    """Tag live/acoustic/remix albums (legacy parity).

    Live/acoustic albums: rename tracks with a ``(Live)`` / ``(Acoustic)``
    suffix, inject the genre, set ``is_live`` / ``is_acoustic`` and
    ``album_context_live``. Remix albums: inject a ``Remix`` genre and set
    ``is_remix`` (no title rename).
    """
    lower = (album_type or "").lower()
    is_live_album = "+live" in lower or "(live)" in lower
    is_remix_album = "+remix" in lower or "(remix)" in lower

    if is_live_album:
        live_type = detect_live_album_type(album, album_type) or "live"
        label = "Acoustic" if live_type == "acoustic" else "Live"
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
                cursor.execute(
                    """
                    UPDATE tracks
                    SET is_live = %s, is_acoustic = %s, album_context_live = 1, title = %s
                    WHERE id = %s
                    """,
                    (1 if label == "Live" else 0, 1 if label == "Acoustic" else 0, new_title, str(track_id)),
                )
                _inject_album_genre(conn, cursor, str(track_id), label, track.get("musicbrainz_genres"), track.get("genres"))
            except Exception as exc:
                logger.debug("[album_stage] Live tagging failed for %s: %s", track_id, exc)
        conn.commit()
        logger.info("[album_stage] Tagged '%s - %s' as %s album", artist, album, label)

    if is_remix_album:
        for track in tracks or []:
            track_id = track.get("id")
            if not track_id:
                continue
            try:
                cursor.execute(
                    "UPDATE tracks SET is_remix = 1 WHERE id = %s AND COALESCE(is_remix, 0) = 0",
                    (str(track_id),),
                )
                _inject_album_genre(conn, cursor, str(track_id), "Remix", track.get("musicbrainz_genres"), track.get("genres"))
            except Exception as exc:
                logger.debug("[album_stage] Remix tagging failed for %s: %s", track_id, exc)
        conn.commit()
        logger.info("[album_stage] Tagged '%s - %s' as remix album", artist, album)


# Trailing live/acoustic suffix the album stage appends when tagging live
# albums ("Song (Live)", "Song (Acoustic)").
_LIVE_SUFFIX_RE = re.compile(r"\s*\((?:Live|Acoustic)[^)]*\)\s*$", re.IGNORECASE)


def strip_live_acoustic_suffix(title: str) -> str:
    """Remove a trailing ``(Live ...)`` / ``(Acoustic ...)`` suffix."""
    return _LIVE_SUFFIX_RE.sub("", title or "").strip()


def _drop_live_genres_from_json(raw: Any) -> str | None:
    """Remove injected ``Live``/``Acoustic`` entries from a JSON genre list.

    Returns the new JSON string, or None when nothing changed.
    """
    try:
        if not raw:
            return None
        parsed = json.loads(raw) if isinstance(raw, str) else list(raw)
        kept = [g for g in parsed if str(g).strip().lower() not in ("live", "acoustic")]
        return json.dumps(kept) if kept != parsed else None
    except Exception:
        return None


def _drop_live_genres_from_csv(raw: Any) -> str | None:
    """Remove injected ``Live``/``Acoustic`` entries from a comma list."""
    try:
        if not raw:
            return None
        parts = [g.strip() for g in str(raw).split(",") if g.strip()]
        kept = [g for g in parts if g.lower() not in ("live", "acoustic")]
        return ", ".join(kept) if kept != parts else None
    except Exception:
        return None


def revert_track_live_state(track_id: str) -> bool:
    """Undo album-stage live/acoustic tagging for a single track.

    Called when a track is un-marked live (track edit) or its album type
    changes away from live (album edit): strips the appended
    ``(Live)``/``(Acoustic)`` suffix, clears ``is_live`` / ``is_acoustic`` /
    ``album_context_live``, removes the injected ``Live``/``Acoustic`` genre
    and rewrites the audio file tags. Returns True when anything changed.
    """
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
                logger.debug("[album_stage] Live revert tag write failed for %s: %s", track_id, tag_err)

        logger.info("[album_stage] Reverted live/acoustic tagging for track %s ('%s' → '%s')",
                    track_id, old_title, new_title or old_title)
        return True
    except Exception as exc:
        logger.debug("[album_stage] Live-state revert failed for %s: %s", track_id, exc)
        return False


def _persist_alternate_takes(conn, cursor, album_context) -> None:
    """Mark alternate takes (``alternate_take`` / ``base_track_id``)."""
    alternate_takes = (album_context or {}).get("alternate_takes") or {}
    updated = 0
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
                    cursor.execute(
                        "UPDATE tracks SET alternate_take = 1, base_track_id = %s WHERE id = %s AND COALESCE(alternate_take, 0) = 0",
                        (str(base_id), str(alt_id)),
                    )
                    updated += 1
                except Exception as exc:
                    logger.debug("[album_stage] Alternate-take persist failed for %s: %s", alt_id, exc)
    if updated:
        conn.commit()
        logger.info("[album_stage] Marked %s alternate take(s) in album", updated)


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
    spotify_type = str(
        album_row.get("musicbrainz_album_type")
        or album_row.get("spotify_album_type")
        or ""
    )

    # Scan-mode gates: a singles pass only needs the album type (compilation
    # detection feeds singles detection) and the MusicBrainz artist ID (the
    # artist-scoped release-group search); art / bio / country / similar
    # artists / Last.fm tags / live-remix tagging are NOT singles work. A
    # popularity-only pass needs none of the enrichment at all — popularity
    # scoring reads raw counts, not album enrichment — so only the free
    # name-based type detection runs (no API calls).
    _singles_pass = bool(
        options.get("singles_only")
        or options.get("singles_with_missing_popularity")
        or options.get("singles_detection_only")
    )
    _popularity_pass = bool(options.get("popularity_only"))
    _meta = {"country": None, "bio": None, "image_url": None}
    _similar: dict[str, list] = {"lastfm": [], "listenbrainz": []}

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
    cursor = conn.cursor()
    album_tracks = album_row.get("tracks") or []
    try:
        # 1. Album type detection (name-based + MusicBrainz release-group).
        # A popularity-only pass runs the free name-based check only — the MB
        # release-group lookup and the persistence write are not required to
        # score popularity (and the persist could clobber a stronger type that
        # a full scan detected). A singles pass keeps the MB lookup: the
        # single/EP verdict feeds singles detection's compilation/track-count
        # gates.
        detected_type = _detect_album_type(artist, album, album_artist or None, spotify_type or None)
        if _popularity_pass:
            is_hetero = any(m in detected_type.lower() for m in _HETEROGENEOUS_MARKERS)
        else:
            mb_type, rg_mbid = _lookup_musicbrainz_album_type(artist, album)
            if mb_type:
                track_count = len(album_tracks)
                # A MusicBrainz 'single'/'ep' verdict for a large track count is
                # almost certainly a full album — downgrade (legacy EP override).
                if mb_type in ("single", "ep") and track_count > 6:
                    mb_type = "album"
                elif mb_type == "single" and track_count > 3:
                    mb_type = "ep"
                # Prefer the MusicBrainz verdict for single/EP (name-based heuristics
                # cannot detect those) and let it refine a plain 'album' verdict;
                # keep name-based live/soundtrack/compilation verdicts otherwise.
                if detected_type == "album" or mb_type in ("single", "ep"):
                    detected_type = mb_type
                    logger.info("[album_stage] MB album type for '%s - %s': %s", artist, album, detected_type)
            is_hetero = any(m in detected_type.lower() for m in _HETEROGENEOUS_MARKERS)
            logger.info("[album_stage] '%s - %s' → type=%s, heterogeneous=%s",
                         artist, album, detected_type, is_hetero)

            # Propagate the detected type + release-group MBID to album tracks.
            _persist_album_type_to_tracks(conn, cursor, artist, album, album_tracks, detected_type, rg_mbid)

            # Mark compilation/soundtrack albums (legacy is_compilation flag).
            if "+compilation" in detected_type.lower() or "+soundtrack" in detected_type.lower():
                try:
                    cursor.execute(
                        "UPDATE tracks SET is_compilation = 1 "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s "
                        "AND COALESCE(is_compilation, 0) = 0",
                        (artist, album),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.debug("[album_stage] Compilation flag persist failed: %s", exc)

        # 2. Album art / artist metadata / similar artists / Last.fm tags /
        #    Discogs id / live-remix tagging — enrichment, not singles or
        #    popularity work.  A singles pass keeps ONLY the MusicBrainz artist
        #    ID (the artist-scoped release-group search feeds singles
        #    detection); a popularity-only pass skips all of it.
        if _popularity_pass or _singles_pass:
            art_source = None
            meta = _meta
            similar = _similar
            if _singles_pass:
                _fetch_musicbrainz_artist_id(artist, conn, options)
        else:
            # 2. Album art (delegates to existing enrichment services)
            art_source = _fetch_album_art_with_fallback(artist, album, discogs_token)
            if art_source:
                logger.info("[album_stage] Album art cached for %s - %s (%s)", artist, album, art_source)

            # 3. Artist metadata (delegates to existing enrichment services)
            meta = _fetch_artist_metadata(artist, conn)

            # Last.fm artist top tags (legacy parity)
            _fetch_artist_lastfm_tags(artist, conn)

            # Backfill releasecountry for tracks missing a release country so
            # Navidrome's "Release Country" field stays populated (legacy parity).
            if meta.get("country"):
                try:
                    cursor.execute(
                        "UPDATE tracks SET releasecountry = %s "
                        "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s "
                        "AND (releasecountry IS NULL OR TRIM(releasecountry) = '')",
                        (meta["country"], artist),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.debug("[album_stage] releasecountry backfill failed: %s", exc)

            # 4. MusicBrainz artist ID — run BEFORE similar artists so the LB
            #    similar-artists lookup can use the freshly resolved MBID.
            _fetch_musicbrainz_artist_id(artist, conn, options)

            # 5. Similar artists
            similar = _fetch_similar_artists(artist, conn, options)

            # 6. Discogs artist ID (legacy parity)
            _fetch_discogs_artist_id(artist, conn, options)

            # 7. Live/remix album tagging + alternate-take persistence
            _apply_live_remix_album_tagging(conn, cursor, artist, album, detected_type, album_tracks)
            _persist_alternate_takes(conn, cursor, album_context)

        # 8. Cover song detection (legacy parity — full CoverDetector).
        # NOTE: the full cover pass (work-history lookup + "Title (Artist
        # Cover)" rename) runs as its OWN stage AFTER the per-track singles
        # detection loop in ``scan_stage_runner`` — keeping this album
        # enrichment section fast (no serial cover API lookups right after
        # album art caching). The per-track ``detect_cover_song`` check in
        # track_stage still runs here implicitly via the track loop.
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

