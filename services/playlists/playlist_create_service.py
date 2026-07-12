"""Navidrome playlist file creator.

Creates ``.nsp`` (Navidrome Smart Playlist) files on disk for the
Navidrome playlist system.

Key Functions:
    - create_playlist_file(): Write a playlist JSON (.nsp) file to the
      music library Playlists directory.

Used by:
    - Essential-artist playlist generation.
    - Manual playlist creation workflows.
    - ``playlist_service.create_or_update_playlist_for_artist()``

File Format:
    NSP files are JSON with keys: ``name``, ``comment``, ``trackIds``.
    Navidrome watches the Playlists directory and loads new `.nsp` files
    automatically.
"""

import os
import json
from services.playlists.playlist_service import sanitize_playlist_name

def create_playlist_file(name, description, track_ids):
    music_folder = os.environ.get("MUSIC_FOLDER", "/music")
    playlists_dir = os.path.join(music_folder, "Playlists")

    os.makedirs(playlists_dir, exist_ok=True)

    safe_name = sanitize_playlist_name(name)
    file_path = os.path.join(playlists_dir, f"{safe_name}.nsp")

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump({
            "name": name,
            "comment": description,
            "trackIds": track_ids
        }, f, indent=2)

    return file_path