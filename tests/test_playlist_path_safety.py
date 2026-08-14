"""Tests for playlist path-safety (path-traversal guard).

The playlist read/export/rename routes accept a user-supplied ``file_path``.
``is_safe_playlist_path`` must reject anything outside the Playlists
directory (including parent traversal and symlink escapes), and accept real
``.nsp`` / ``.m3u`` files inside it.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def playlists_env(monkeypatch):
    """Point MUSIC_FOLDER at a temp dir with a Playlists subfolder."""
    tmp = tempfile.mkdtemp()
    music = os.path.join(tmp, "music")
    playlists = os.path.join(music, "Playlists")
    os.makedirs(playlists, exist_ok=True)

    # A real playlist file inside the root.
    safe_file = os.path.join(playlists, "Album.nsp")
    with open(safe_file, "w", encoding="utf-8") as handle:
        handle.write('{"name": "Album", "trackIds": []}')

    # A decoy file OUTSIDE the Playlists root that traversal could target.
    secret = os.path.join(tmp, "secret.txt")
    with open(secret, "w", encoding="utf-8") as handle:
        handle.write("top secret")

    monkeypatch.setenv("MUSIC_FOLDER", music)
    return {
        "playlists": playlists,
        "safe_file": safe_file,
        "secret": secret,
    }


def test_safe_playlist_path_accepts_internal_file(playlists_env):
    from services.playlists.playlist_service import is_safe_playlist_path
    assert is_safe_playlist_path(playlists_env["safe_file"]) is True


def test_safe_playlist_path_rejects_arbitrary_file(playlists_env):
    from services.playlists.playlist_service import is_safe_playlist_path
    # A file elsewhere on disk (e.g. /etc/passwd-style target) must be rejected.
    assert is_safe_playlist_path(playlists_env["secret"]) is False
    assert is_safe_playlist_path(os.path.join(playlists_env["playlists"], "..", "secret.txt")) is False


def test_safe_playlist_path_rejects_empty_and_missing(playlists_env):
    from services.playlists.playlist_service import is_safe_playlist_path
    assert is_safe_playlist_path("") is False
    assert is_safe_playlist_path(os.path.join(playlists_env["playlists"], "nope.nsp")) is False
