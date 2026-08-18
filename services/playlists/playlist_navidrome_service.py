"""Navidrome playlist API wrapper.

Thin wrapper around ``NavidromeClient`` playlist operations for
interacting with the Navidrome Subsonic API.

Key Functions:
    - list_playlists(): Fetch all playlists from Navidrome.
    - load_playlist(): Get detailed playlist contents by ID.
    - create_navidrome_playlist(): Create a new playlist via the
      Subsonic ``createPlaylist`` endpoint.
    - sync_playlist_by_name(): Create OR update (in place) a playlist,
      deleting same-name duplicates so scans never recreate entries.

Used by:
    - Playlist WebUI routes for browsing and managing playlists.
    - Playlist sync workflows (Essential Collections, Genre Top Tracks,
      New Music — every generated playlist that must not duplicate).

Architecture:
    Delegates HTTP calls to ``api_clients.navidrome.NavidromeClient``.
    No business logic — pure API wrapper.
"""

from __future__ import annotations

import logging
from typing import Any

from api_clients.navidrome import NavidromeClient

logger = logging.getLogger(__name__)


def list_playlists(base_url, user, password):
    client = NavidromeClient(base_url, user, password)
    return client.fetch_all_playlists()


def load_playlist(base_url, user, password, playlist_id):
    client = NavidromeClient(base_url, user, password)
    return client.fetch_playlist(playlist_id)


def create_navidrome_playlist(base_url, user, password, name, tracks):
    """Create a playlist via Navidrome Subsonic API."""
    client = NavidromeClient(base_url, user, password)
    return client._get_subsonic_response(
        "createPlaylist",
        params={"name": name, "songId": tracks},
    )


def sync_playlist_by_name(
    client: NavidromeClient,
    name: str,
    song_ids: list[str],
) -> dict[str, Any]:
    """Create or update (IN PLACE) a Navidrome playlist by name.

    The generated playlists (Essential Collection, ``{Genre} - Top Tracks``,
    New Music) are written as ``.m3u`` files into the watch folder; Navidrome
    imports each file change as a NEW playlist, so every scan that rewrote the
    file left a duplicate behind.  This syncs the Navidrome playlist to match
    the file WITHOUT recreating it:

    1. Find every playlist with the same name.
    2. If more than one exists → delete the extras (dedupe).
    3. If one exists → ``updatePlaylist`` replaces its song list in place
       (old tracks that dropped below the threshold are removed, new tracks
       are added, order follows the provided ``song_ids``).
    4. If none exists → create it with the song list.

    Returns ``{"updated": bool, "created": bool, "deduped": int,
    "playlist_id": str}``.  Never raises.
    """
    result: dict[str, Any] = {"updated": False, "created": False, "deduped": 0, "playlist_id": ""}
    if not client or not name:
        return result
    try:
        all_playlists = client.fetch_all_playlists() or []
        wanted = str(name or "").strip().lower()
        matches = [
            p for p in all_playlists
            if str(p.get("name") or "").strip().lower() == wanted
        ]
        # Smart playlists (from .nsp files) carry the same name — leave them
        # alone; only sync the REGULAR (file-imported) playlists.
        matches = [p for p in matches if not _is_smart(p)]
    except Exception as exc:
        logger.warning("[PLAYLISTS] sync_playlist_by_name lookup failed for '%s': %s", name, exc)
        return result

    # Dedupe: keep the first regular playlist, delete the rest.
    primary = matches[0] if matches else None
    for dup in matches[1:]:
        dup_id = str(dup.get("id") or "")
        if dup_id and dup_id != (primary or {}).get("id"):
            try:
                if client.delete_playlist(dup_id):
                    result["deduped"] += 1
                    logger.info(
                        "[PLAYLISTS] Deleted duplicate Navidrome playlist '%s' (%s)",
                        name, dup_id,
                    )
            except Exception as exc:
                logger.warning("[PLAYLISTS] Duplicate deletion failed for '%s': %s", name, exc)

    song_ids = [str(s) for s in (song_ids or []) if str(s or "").strip()]

    if primary:
        pid = str(primary.get("id") or "")
        result["playlist_id"] = pid
        try:
            ok = client.update_playlist_songs(pid, song_ids)
            result["updated"] = bool(ok)
            if ok:
                logger.info(
                    "[PLAYLISTS] Updated Navidrome playlist '%s' in place (%d songs)",
                    name, len(song_ids),
                )
            else:
                logger.warning(
                    "[PLAYLISTS] updatePlaylist failed for '%s' — falling back to recreate? (not implemented: in-place is the contract)",
                    name,
                )
        except Exception as exc:
            logger.warning("[PLAYLISTS] updatePlaylist raised for '%s': %s", name, exc)
        return result

    # No existing regular playlist — create it.
    try:
        data = client._get_subsonic_response(
            "createPlaylist",
            timeout=60,
            params={"name": name, "songId": song_ids},
        )
        pid = str((data.get("playlist") or {}).get("id") or "")
        result["created"] = data.get("status") == "ok"
        result["playlist_id"] = pid
        if result["created"]:
            logger.info("[PLAYLISTS] Created Navidrome playlist '%s' (%d songs)", name, len(song_ids))
        else:
            logger.warning("[PLAYLISTS] createPlaylist failed for '%s': %s", name, data)
    except Exception as exc:
        logger.warning("[PLAYLISTS] createPlaylist raised for '%s': %s", name, exc)
    return result


def _is_smart(playlist: dict[str, Any]) -> bool:
    """True when the playlist is a smart playlist (from .nsp files)."""
    try:
        from api_clients.navidrome import NavidromeClient
        return NavidromeClient._is_smart_playlist(playlist)
    except Exception:
        return bool(playlist.get("smart") or playlist.get("isSmart") or playlist.get("criteria"))