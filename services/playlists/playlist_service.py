"""Smart playlist generation service.

Generates and manages Navidrome Smart Playlist (.nsp) files based on
library content, including:

Key Functions:
    - sanitize_playlist_name(): Strip unsafe characters from names.
    - create_nsp_file(): Write playlist JSON data to disk.
    - create_or_update_playlist_for_artist(): Generate an "Essential"
      playlist for a specific artist.
    - refresh_all_playlists_from_db(): Rebuild all playlists from current
      database ratings.

Architecture:
    Reads tracks with high ratings (>= 4) from the database and generates
    ``.nsp`` files that Navidrome auto-loads from its Playlists directory.
"""
from __future__ import annotations
import json
import os
import re
from sqlalchemy import text
from db.engine import db_session


def sanitize_playlist_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in ("-", "_", " ")).strip()


def playlist_path(playlist_name: str) -> str:
    music_folder = os.environ.get("MUSIC_FOLDER", "/music")
    playlists_dir = os.path.join(music_folder, "Playlists")
    os.makedirs(playlists_dir, exist_ok=True)
    return os.path.join(playlists_dir, f"{sanitize_playlist_name(playlist_name)}.nsp")


def delete_nsp_file(playlist_name: str) -> None:
    path = playlist_path(playlist_name)
    if os.path.exists(path):
        os.remove(path)


def create_nsp_file(playlist_name: str, playlist_data: dict) -> bool:
    try:
        with open(playlist_path(playlist_name), "w", encoding="utf-8") as handle:
            json.dump(playlist_data, handle, indent=2)
        return True
    except Exception:
        return False


def _playlists_dir() -> str:
    return os.path.join(os.environ.get("MUSIC_FOLDER", "/music"), "Playlists")


def list_nsp_playlists() -> list[dict]:
    """Return all smart-playlist .nsp files found in the Playlists directory."""
    playlists_dir = _playlists_dir()
    if not os.path.isdir(playlists_dir):
        return []

    found = []
    for file_name in sorted(os.listdir(playlists_dir)):
        if not file_name.lower().endswith(".nsp"):
            continue
        file_path = os.path.join(playlists_dir, file_name)
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        embedded = data.get("tracks") or []
        track_ids = data.get("trackIds") or []
        embedded_count = len(embedded) if isinstance(embedded, list) else 0
        id_count = len(track_ids) if isinstance(track_ids, list) else 0
        rules = data.get("rules") or {}

        found.append({
            "name": str(data.get("name") or file_name[:-4]),
            "file_name": file_name,
            "file_path": file_path,
            "comment": str(data.get("comment") or ""),
            "rules": rules if isinstance(rules, dict) else {},
            "track_count": embedded_count or id_count,
            "rule_based": bool(rules),
        })
    return found


def read_nsp_playlist(file_path: str) -> dict | None:
    """Read an .nsp file for display.

    Embedded ``tracks`` are normalized to ``{id, title, artist, album,
    rating}`` entries.  Rule-based playlists have no embedded track list,
    so ``_tracks`` is empty and the caller may resolve via Navidrome.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    tracks = []
    embedded = data.get("tracks") or []
    if isinstance(embedded, list):
        for index, entry in enumerate(embedded, start=1):
            if not isinstance(entry, dict):
                continue
            tracks.append({
                "id": str(entry.get("id") or f"{os.path.basename(file_path)}#{index}"),
                "title": str(entry.get("title") or f"Track {index}"),
                "artist": str(entry.get("artist") or ""),
                "album": str(entry.get("album") or ""),
                "rating": entry.get("rating"),
            })

    data["_tracks"] = tracks
    data["_file_path"] = file_path
    data["_file_name"] = os.path.basename(file_path)
    return data


def rename_nsp_playlist(file_path: str, new_name: str, new_file_name: str | None = None) -> dict:
    """Rename a smart playlist: update its embedded ``name`` and rename the
    .nsp file on disk.  Returns ``{name, file_path, file_name}``.

    Raises ValueError for invalid input or an existing target file.
    """
    new_name = str(new_name or "").strip()
    if not new_name:
        raise ValueError("Playlist name is required")

    with open(file_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Playlist file is not valid JSON")

    data["name"] = new_name

    safe_file = str(new_file_name or "").strip()
    safe_file = sanitize_playlist_name(safe_file or new_name)
    if not safe_file:
        raise ValueError("File name is required")
    if not safe_file.lower().endswith(".nsp"):
        safe_file = f"{safe_file}.nsp"

    target_path = os.path.abspath(os.path.join(os.path.dirname(file_path), safe_file))
    if os.path.abspath(file_path) != target_path and os.path.exists(target_path):
        raise ValueError(f"A playlist file named '{safe_file}' already exists")

    with open(file_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)

    if os.path.abspath(file_path) != target_path:
        os.rename(file_path, target_path)

    return {
        "name": new_name,
        "file_path": target_path,
        "file_name": os.path.basename(target_path),
    }


def create_or_update_playlist_for_artist(artist_name: str, tracks: list):
    playlist_name = f"{artist_name} (Essential Playlist)"
    data = {"name": playlist_name, "comment": "Generated by Popularr", "rules": {"artist": artist_name}, "tracks": tracks or []}
    return create_nsp_file(playlist_name, data)


def refresh_all_playlists_from_db():
    with db_session() as session:
        result = session.execute(text("SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS artist FROM tracks WHERE rating >= 4"))
        artists = [str(row[0]) for row in result.fetchall() or [] if row[0]]
        count = 0
        for artist in artists:
            result = session.execute(text("SELECT id, title, rating FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND rating >= 4"), {"artist": artist})
            tracks = [{"id": str(row[0]), "title": str(row[1]), "rating": int(row[2])} for row in result.fetchall() or []]
            if create_or_update_playlist_for_artist(artist, tracks):
                count += 1
        return count
