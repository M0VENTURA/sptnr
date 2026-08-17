"""Artist scan service.

Responsible for external release comparison and scan orchestration.
DB persistence is delegated to repositories; network calls go through
``api_clients.musicbrainz_http.MusicBrainzHttpClient``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.repositories.metadata import (
    fetch_artist_albums,
    fetch_artist_mbid,
    fetch_all_distinct_artists,
)
from helpers.normalization_service import normalize_title_for_lookup
from helpers.musicbrainz_helpers import normalize_single_mbid

logger = logging.getLogger(__name__)

_PROGRESS_PATH = "missing_releases_scan_progress.json"
_scan_thread: threading.Thread | None = None


def _normalize_release_title(title: str) -> str:
    return normalize_title_for_lookup(title or "")


def _fetch_musicbrainz_release_groups(artist_mbid: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Browse release-groups for an artist via the MusicBrainz HTTP client.

    Uses ``browse_artist_release_groups`` (the browse endpoint is the
    recommended way to list an artist's release groups; text search is
    unreliable for this purpose).
    """
    from api_clients.musicbrainz_http import MusicBrainzHttpClient

    client = MusicBrainzHttpClient()
    page = client.browse_artist_release_groups(artist_mbid, limit=limit, offset=offset)
    return page.get("release_groups", []) or []


def _fetch_all_musicbrainz_releases(artist_mbid: str, max_pages: int = 4) -> list[dict[str, Any]]:
    """Fetch all release-groups for an artist, paging through the browse endpoint."""
    releases: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        page = _fetch_musicbrainz_release_groups(artist_mbid, limit=100, offset=offset)
        if not page:
            break
        releases.extend(page)
        if len(page) < 100:
            break
        offset += len(page)
    return releases


def _categorize_release(release_group: dict[str, Any]) -> str:
    """Route a release-group into a display category.

    Mirrors legacy behaviour: EPs and singles stay in their own buckets;
    secondary types decide Compilation / Live Album / Remix; otherwise Album.

    MusicBrainz routinely tags short-form releases as ``primary-type: album``
    with a ``secondary-type`` of ``single`` or ``ep`` (e.g. Poppy's
    *Guardian* single).  Those secondary types are honoured so the release
    lands in the Singles / EPs bucket instead of cluttering Albums.
    """
    primary_type = (release_group.get("primary-type") or release_group.get("primary_type") or "").lower()
    if primary_type not in ("album", "ep", "single"):
        return "Album"

    # The browse/search APIs return ``secondary-types`` as an ARRAY, but the
    # search endpoint can also yield a comma-joined STRING ("Live,Compilation").
    # Iterating a string character-by-character never matches — normalise to a
    # list first (mirrors ``_parse_secondary_types`` in musicbrainz_service).
    raw_secondary = release_group.get("secondary-types") or release_group.get("secondary_types") or []
    if isinstance(raw_secondary, str):
        raw_secondary = [raw_secondary]
    secondary = [
        s.lower()
        for s in raw_secondary
        if isinstance(s, str) and s.strip()
    ]

    if "single" in secondary:
        return "Single"
    if "ep" in secondary:
        return "EP"
    if primary_type == "ep":
        return "EP"
    if primary_type == "single":
        return "Single"
    if "compilation" in secondary:
        return "Compilation"
    if "live" in secondary:
        return "Live Album"
    if "remix" in secondary:
        return "Remix"
    return "Album"


def _release_cover_art_url(release_group: dict[str, Any]) -> str:
    """Build a Cover Art Archive URL for a release-group when artwork exists."""
    rg_id = release_group.get("id") or ""
    if not rg_id:
        return ""
    caa = release_group.get("cover-art-archive") or {}
    if caa.get("artwork") or caa.get("count", 0) > 0:
        return f"https://coverartarchive.org/release-group/{rg_id}/front-500"
    return ""


def _build_missing_release_items(
    release_groups: list[dict[str, Any]],
    existing_norm: set[str],
    include_singles_current_year_only: bool = False,
) -> list[dict[str, Any]]:
    """Filter release-groups into missing-release items.

    Includes ALL release types the artist page displays: Albums, Live Albums,
    Remix Albums, Compilations, EPs and Singles.  Excludes releases already
    in the library and non album/ep/single primary types.

    ``include_singles_current_year_only`` is a legacy escape hatch that can
    be re-enabled to only surface current-year singles; it defaults to False
    so the full singles catalogue is populated.
    """
    from datetime import datetime

    now_year = datetime.now().year
    missing: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for rg in release_groups:
        title = rg.get("title") or ""
        norm_title = _normalize_release_title(title)
        if not norm_title or norm_title in existing_norm:
            continue

        primary_type = (rg.get("primary-type") or rg.get("primary_type") or "").lower()
        if primary_type not in ("album", "ep", "single"):
            continue

        category = _categorize_release(rg)

        # Only include singles released in the current calendar year when the
        # legacy flag is explicitly enabled (default: include all singles).
        if category == "Single" and include_singles_current_year_only:
            first_release = (rg.get("first-release-date") or rg.get("first_release_date") or "")
            try:
                release_year = int(first_release.split("-")[0])
            except (ValueError, TypeError):
                release_year = 0
            if release_year < now_year:
                continue

        dedupe_key = (norm_title, category)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        release_id = rg.get("id", "") or f"{norm_title}-{category.lower()}"
        missing.append({
            "id": release_id,
            "title": title,
            "primary_type": rg.get("primary-type", rg.get("primary_type", "")),
            "first_release_date": rg.get("first-release-date", rg.get("first_release_date", "")),
            "cover_art_url": _release_cover_art_url(rg),
            "category": category,
        })

    return missing


def _persist_missing_releases(artist: str, missing_items: list[dict[str, Any]]) -> None:
    """Replace the artist's cached missing releases in the DB (delete + insert)."""
    if not artist:
        return
    with db_session() as session:
        session.execute(
            text("DELETE FROM missing_releases WHERE LOWER(artist) = LOWER(:artist)"),
            {"artist": artist},
        )
        for item in missing_items:
            session.execute(
                text("""
                    INSERT INTO missing_releases
                        (artist, release_id, title, primary_type, first_release_date,
                         cover_art_url, category, last_checked)
                    VALUES (:artist, :release_id, :title, :primary_type,
                            :first_release_date, :cover_art_url, :category, CURRENT_TIMESTAMP)
                """),
                {
                    "artist": artist,
                    "release_id": item.get("id", ""),
                    "title": item.get("title", ""),
                    "primary_type": item.get("primary_type", "Album"),
                    "first_release_date": item.get("first_release_date", ""),
                    "cover_art_url": item.get("cover_art_url", ""),
                    "category": item.get("category", "Album"),
                },
            )


def _cleanup_imported_releases() -> int:
    """Remove cached missing releases that have since been imported into the library."""
    with db_session() as session:
        result = session.execute(text("""
            DELETE FROM missing_releases mr
            WHERE EXISTS (
                SELECT 1 FROM tracks t
                WHERE LOWER(COALESCE(NULLIF(t.album_artist, ''), t.artist)) = LOWER(mr.artist)
                  AND LOWER(TRIM(t.album)) = LOWER(TRIM(mr.title))
            )
        """))
        return result.rowcount or 0


def _resolve_artist_mbid(artist: str, conn) -> str | None:
    """Return a stable artist MBID, falling back to a MusicBrainz lookup.

    Mirrors legacy resolution order:
    1. Most-common ``musicbrainz_albumartistid`` from tracks
    2. ``fetch_artist_mbid`` (album_artist based)
    3. ``lookup_and_save_artist_mbid`` (live MusicBrainz search)
    """
    from collections import Counter

    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text("""
                    SELECT COALESCE(
                        NULLIF(TRIM(musicbrainz_albumartistid), ''),
                        NULLIF(TRIM(musicbrainz_artistid), '')
                    ) AS mbid
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND COALESCE(
                        NULLIF(TRIM(musicbrainz_albumartistid), ''),
                        NULLIF(TRIM(musicbrainz_artistid), '')
                      ) IS NOT NULL
                """),
                {"artist": artist},
            ).fetchall()
        mbids = []
        for row in rows:
            raw = row[0]
            if raw:
                normalized = normalize_single_mbid(str(raw))
                if normalized:
                    mbids.append(normalized)
        if mbids:
            return Counter(mbids).most_common(1)[0][0]
    except Exception as exc:
        logger.debug("[MISSING_RELEASES] MBID query failed for '%s': %s", artist, exc)

    try:
        mbid = fetch_artist_mbid(None, artist)
        if mbid:
            return normalize_single_mbid(mbid) or mbid
    except Exception as exc:
        logger.debug("[MISSING_RELEASES] fetch_artist_mbid failed for '%s': %s", artist, exc)

    try:
        from services.enrichment.musicbrainz_persistence_service import lookup_and_save_artist_mbid
        mbid = lookup_and_save_artist_mbid(artist)
        return normalize_single_mbid(mbid) or mbid
    except Exception as exc:
        logger.debug("[MISSING_RELEASES] Artist MBID lookup failed for '%s': %s", artist, exc)
    return None


def _scan_is_running() -> bool:
    """Return True when the full-library missing-releases scan is active."""
    return bool(_scan_thread and _scan_thread.is_alive())


def _cached_missing_releases(artist: str) -> list[dict[str, Any]]:
    """Read the artist's cached missing releases from the DB."""
    if not artist:
        return []
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT release_id, title, primary_type, first_release_date,
                       cover_art_url, category, last_checked
                FROM missing_releases
                WHERE LOWER(artist) = LOWER(:artist)
                ORDER BY first_release_date DESC NULLS LAST, title ASC
            """),
            {"artist": artist},
        )
        return [dict(r._mapping) for r in result.fetchall() or []]


def get_missing_releases(artist: str, background: bool = False):
    """Detect missing releases for an artist and persist the results.

    Compares the local library against MusicBrainz release-groups, caches the
    result in ``missing_releases`` (so the artist page and dashboard read from
    the cache), and returns the fresh list.

    When ``background`` is True and a full-library missing-releases scan is
    already running, returns the cached results instead of performing a live
    MusicBrainz lookup (avoids contending with the active scan).
    """
    if not artist:
        return {"error": "Artist is required"}, 400

    if background and _scan_is_running():
        cached = _cached_missing_releases(artist)
        return {
            "artist": artist,
            "missing": [
                {
                    "id": r.get("release_id", ""),
                    "title": r.get("title", ""),
                    "primary_type": r.get("primary_type", "Album"),
                    "first_release_date": str(r.get("first_release_date", "")),
                    "cover_art_url": r.get("cover_art_url", ""),
                    "category": r.get("category", "Album"),
                }
                for r in cached
            ],
            "from_cache": True,
            "scan_guarded": True,
        }, 200

    try:
        existing_albums = fetch_artist_albums(None, artist)
        artist_mbid = _resolve_artist_mbid(artist, None)
    except Exception as exc:
        logger.error("[MISSING_RELEASES] get_missing_releases failed for %s: %s", artist, exc)
        return {"artist": artist, "missing": [], "existing_albums": [], "info": str(exc)}, 500

    existing_norm = {_normalize_release_title(a) for a in existing_albums if a}

    if not artist_mbid:
        return {
            "artist": artist,
            "missing": [],
            "existing_albums": existing_albums,
            "info": "No MusicBrainz artist ID stored for this artist. Run a popularity scan first to resolve the MBID.",
        }, 200

    try:
        release_groups = _fetch_all_musicbrainz_releases(artist_mbid)
    except Exception as exc:
        logger.error("[MISSING_RELEASES] MusicBrainz fetch failed for %s: %s", artist, exc)
        return {"artist": artist, "missing": [], "existing_albums": existing_albums, "info": str(exc)}, 500

    missing_items = _build_missing_release_items(release_groups, existing_norm)

    try:
        _persist_missing_releases(artist, missing_items)
    except Exception as exc:
        logger.warning("[MISSING_RELEASES] Could not persist missing releases for %s: %s", artist, exc)

    return {
        "artist": artist,
        "missing": missing_items,
        "existing_albums": existing_albums,
    }, 200


def _run_missing_releases_scan() -> None:
    """Background loop: scan every library artist for missing releases."""
    global _scan_thread
    try:
        from services.scanning.scan_state import (
            clear_stop_request,
            is_stop_requested,
            write_progress_with_current_artist,
        )
        clear_stop_request(_PROGRESS_PATH)

        try:
            artists = fetch_all_distinct_artists(None)
            # Prefer canonical album_artist identities (avoids scanning featured artists).
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            with _db_session() as session:
                rows = session.execute(
                    _text("""
                        SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS canonical_artist
                        FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL
                          AND COALESCE(NULLIF(album_artist, ''), artist) != ''
                        ORDER BY canonical_artist
                    """)
                ).fetchall()
            if rows:
                artists = [str(r[0]) for r in rows]
                artists = [a for a in artists if a]
        except Exception as exc:
            logger.debug("[MISSING_RELEASES] Artist list fetch failed: %s", exc)

        total_artists = len(artists)
        logger.info("[MISSING_RELEASES] Starting scan for %s artists", total_artists)

        try:
            cleaned = _cleanup_imported_releases()
            if cleaned:
                logger.info("[MISSING_RELEASES] Cleaned up %s releases imported since last scan", cleaned)
        except Exception as exc:
            logger.debug("[MISSING_RELEASES] Cleanup failed: %s", exc)

        total_missing = 0
        processed = 0
        for artist in artists:
            if is_stop_requested(_PROGRESS_PATH):
                logger.info("[MISSING_RELEASES] Stop signal received, exiting gracefully")
                write_progress_with_current_artist(
                    _PROGRESS_PATH, "missing_releases_scan", False,
                    extra={"status": "stopped", "processed_artists": processed,
                           "total_artists": total_artists, "total_missing_found": total_missing,
                           "percent_complete": int((processed / total_artists) * 100) if total_artists else 0},
                )
                return

            processed += 1
            write_progress_with_current_artist(
                _PROGRESS_PATH, "missing_releases_scan", True,
                current_artist=artist,
                extra={"status": "running", "processed_artists": processed,
                       "total_artists": total_artists, "total_missing_found": total_missing,
                       "percent_complete": int((processed / total_artists) * 100) if total_artists else 0},
            )

            try:
                artist_mbid = _resolve_artist_mbid(artist, None)
                existing_albums = fetch_artist_albums(None, artist)

                existing_norm = {_normalize_release_title(a) for a in existing_albums if a}
                if not artist_mbid:
                    continue

                release_groups = _fetch_all_musicbrainz_releases(artist_mbid)
                missing_items = _build_missing_release_items(release_groups, existing_norm)
                if missing_items:
                    _persist_missing_releases(artist, missing_items)
                    total_missing += len(missing_items)
            except Exception as exc:
                logger.error("[MISSING_RELEASES] Error scanning %s: %s", artist, exc)
                continue
            finally:
                # MusicBrainz rate limit: ~1 request per second.
                time.sleep(1.1)

        write_progress_with_current_artist(
            _PROGRESS_PATH, "missing_releases_scan", False,
            extra={"status": "complete", "processed_artists": total_artists,
                   "total_artists": total_artists, "total_missing_found": total_missing,
                   "percent_complete": 100},
        )
        logger.info("[MISSING_RELEASES] Scan complete. Found %s missing releases across %s artists", total_missing, total_artists)
    except Exception as exc:
        logger.error("[MISSING_RELEASES] Scan failed: %s", exc, exc_info=True)
        try:
            from services.scanning.scan_state import write_progress_with_current_artist
            write_progress_with_current_artist(
                _PROGRESS_PATH, "missing_releases_scan", False,
                extra={"status": "error", "error": str(exc)},
            )
        except Exception:
            pass
    finally:
        _scan_thread = None


def start_missing_release_scan():
    """Start the full-library missing-releases scan in the background."""
    global _scan_thread
    if _scan_thread and _scan_thread.is_alive():
        return {"success": False, "error": "Missing releases scan already running"}, 400
    _scan_thread = threading.Thread(target=_run_missing_releases_scan, daemon=True, name="missing-release-scan")
    _scan_thread.start()
    return {"success": True, "message": "Missing releases scan started"}, 200


def import_release(artist: str, release_id: str, title: str):
    """Import a missing release as placeholder track records.

    Supports BOTH sources that populate ``missing_releases``:

    - **MusicBrainz** (UUID release or release-group MBID): fetched via the
      MusicBrainz client, with a release-group → concrete-release fallback
      (the artist page hands over release-group MBIDs; ``/release/{id}`` 404s
      for those).
    - **Discogs** (integer release ID): fetched via the Discogs client — the
      Discogs release cache also seeds ``missing_releases`` with integer IDs.

    Each track is saved via ``save_to_db`` with the full legacy record shape
    (a stable ``id`` is REQUIRED — the previous refactor dropped it, which
    would raise "track must include id").
    """
    from datetime import datetime

    artist = str(artist or "").strip()
    release_id = str(release_id or "").strip()
    title = str(title or "").strip()

    if not artist or not release_id or not title:
        return {"error": "Artist, release_id, and title are required"}, 400

    # Discogs release IDs are integers (e.g. "6834679"); MusicBrainz IDs are
    # UUIDs.  Route by shape so a Discogs-sourced missing release imports
    # instead of 404ing against MusicBrainz.
    import re
    is_mb_uuid = bool(re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        release_id, re.IGNORECASE,
    ))

    release_year = ""
    media: list[dict] = []

    if is_mb_uuid:
        from api_clients.musicbrainz_http import MusicBrainzHttpClient
        client = MusicBrainzHttpClient()
        data, _resolved = _resolve_mb_release(client, release_id)
        if not data:
            return {"error": "Release not found on MusicBrainz"}, 404
        release_year = str(data.get("date") or "")[:4]
        media = data.get("media") or []
    else:
        try:
            from api_clients.discogs import DiscogsClient
            from helpers.config_helpers import get_config as _get_cfg
            _token = (_get_cfg().get("api_integrations", {}).get("discogs", {}) or {}).get("token") or ""
            client = DiscogsClient(token=_token)
            data = client.get_release(release_id)
        except Exception as exc:
            logger.debug("[MISSING_RELEASES] Discogs release fetch failed for %s: %s", release_id, exc)
            data = None
        if not data:
            return {"error": "Release not found on Discogs"}, 404
        release_year = str(data.get("year") or "")
        # Discogs tracklist: [{position, title, duration}] — one flat list.
        media = [{"tracks": [
            {"title": t.get("title", ""), "duration": t.get("duration"),
             "position": t.get("position")}
            for t in (data.get("tracklist") or [])
        ]}]

    if not media or not any((d.get("tracks") or []) for d in media):
        return {"error": "No media found"}, 400

    try:
        from db.repositories.popularity_repository import save_to_db
    except Exception:
        from db.repositories.popularity_repository import save_to_db  # compatibility fallback

    count = 0
    for disc_idx, disc in enumerate(media, start=1):
        disc_number = disc.get("position", disc_idx)
        for track_idx, track in enumerate(disc.get("tracks", []), start=1):
            recording = track.get("recording", {})
            track_title = recording.get("title") or track.get("title") or "Unknown"
            duration = track.get("length") or recording.get("length")
            if isinstance(duration, str) and duration.isdigit():
                duration = int(duration)
            mbid = recording.get("id", "")
            # Legacy record shape — ``id`` is REQUIRED by save_to_db; fall back
            # to a stable composite when the recording has no MBID.
            track_record = {
                "id": mbid or f"{release_id}_{disc_number}_{track_idx}",
                "title": track_title,
                "artist": artist,
                "album": title,
                "track_number": track_idx,
                "disc_number": disc_number,
                "duration": duration,
                "year": release_year,
                "mbid": mbid,
                "writer": "[]",
                "score": 0.0,
                "spotify_score": 0,
                "lastfm_score": 0,
                "age_score": 0,
                "genres": "[]",
                "file_path": None,
                "stars": 0,
                "last_scanned": datetime.now().isoformat(),
            }
            save_to_db(track_record)
            count += 1

    # Remove the imported release from the missing-releases cache.
    try:
        with db_session() as session:
            session.execute(
                text("""
                    DELETE FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist) AND release_id = :release_id
                """),
                {"artist": artist, "release_id": release_id},
            )
    except Exception as exc:
        logger.debug("[MISSING_RELEASES] Could not clear imported release from cache: %s", exc)

    return {"success": True, "tracks_imported": count, "message": f"Imported {count} tracks from '{title}'"}, 200


def _resolve_mb_release(client, mb_id: str) -> tuple[dict | None, str]:
    """Resolve a MusicBrainz release OR release-group MBID to a release payload.

    ``get_release`` raises ``httpx.HTTPStatusError`` on a 404 (release-group
    MBIDs from the artist page), so the fallback browses the release-group for
    a concrete release (mirrors download_folder_service._resolve_release).
    """
    try:
        data = client.get_release(mb_id, inc="recordings")
        if data and data.get("id"):
            return data, str(data["id"])
    except Exception:
        pass
    try:
        release_search = client.get(
            "release",
            params={"release-group": mb_id, "limit": 1, "fmt": "json"},
            timeout=15.0,
        )
        releases = (release_search or {}).get("releases") or []
        if not releases:
            return None, ""
        release_mbid = str(releases[0].get("id") or "")
        if not release_mbid:
            return None, ""
        data = client.get_release(release_mbid, inc="recordings")
        return (data, release_mbid) if data else (None, "")
    except Exception as exc:
        logger.debug("[MISSING_RELEASES] release-group resolution failed for %s: %s", mb_id, exc)
        return None, ""


def scan_all_missing_releases() -> tuple[dict, int]:
    """Scan all artists for missing releases in the background."""
    return start_missing_release_scan()


def add_artist(artist: str) -> tuple[dict, int]:
    """Add an artist to the database by creating a placeholder record."""
    if not artist:
        return {"success": False, "error": "artist required"}, 400
    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            session.execute(
                text("INSERT INTO artists (name) VALUES (:artist) ON CONFLICT (name) DO NOTHING"),
                {"artist": artist},
            )
        return {"success": True, "message": f"Artist '{artist}' added"}, 200
    except Exception as exc:
        return {"success": False, "error": str(exc)}, 500
