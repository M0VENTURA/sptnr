"""Tests for the per-track rename endpoint (album page "Rename Selected").

Regression: the album page renders every track in a desktop AND a mobile row,
so "select all" returned each track id TWICE and every file was renamed
twice — the second rename re-renders the same destination and the " (1)"
dedupe suffix produces ``01. Artist - Song (1).mp3``-style files that looked
like deletions.

Second regression: Navidrome imports store RELATIVE file paths
("Artist/Album/01 - Song.mp3"), which the route never resolved against the
music root — the existence check failed with "File not found" and the rename
was a silent no-op for the standard import layout (and the "unchanged"
shortcut could never match a relative source against an absolute dest).
"""

from __future__ import annotations

import os


def _seed_track(file_path: str, *, title: str = "Song", year: int = 2024):
    from db.engine import db_session
    from sqlalchemy import text

    with db_session() as session:
        session.execute(
            text(
                "INSERT INTO tracks (id, artist, album_artist, album, title, "
                "track_number, year, file_path) "
                "VALUES ('track-1', 'Artist', 'Artist', 'Album', :title, "
                "1, :year, :file_path)"
            ),
            {"title": title, "year": year, "file_path": file_path},
        )


async def test_track_rename_resolves_relative_path(app, client, tmp_path, monkeypatch):
    """A Navidrome-style relative file_path is resolved under MUSIC_ROOT."""
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path))
    os.makedirs(tmp_path / "Artist" / "Album")
    src_file = tmp_path / "Artist" / "Album" / "01 - Song.mp3"
    src_file.write_bytes(b"fake mp3 payload")

    _seed_track("Artist/Album/01 - Song.mp3")

    r = await client.post("/api/track/track-1/rename-file")
    data = await r.get_json()

    assert data["success"] is True
    assert data["renamed"] is True
    assert not (tmp_path / "Artist" / "Album" / "01 - Song.mp3").exists()
    moved = tmp_path / "Artist" / "2024 - Album" / "01. Artist - Song.mp3"
    assert moved.exists()
    assert moved.read_bytes() == b"fake mp3 payload"


async def test_track_rename_second_call_is_unchanged(app, client, tmp_path, monkeypatch):
    """Re-running the rename (double selection) must NOT produce " (1)" files.

    After the first rename the DB holds the new (relative) path; a second
    call with the same track id renders the identical destination and must
    short-circuit as unchanged instead of deduping to a " (1)" copy.
    """
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path))
    os.makedirs(tmp_path / "Artist" / "Album")
    (tmp_path / "Artist" / "Album" / "01 - Song.mp3").write_bytes(b"payload")

    _seed_track("Artist/Album/01 - Song.mp3")

    first = await (await client.post("/api/track/track-1/rename-file")).get_json()
    assert first["success"] is True and first["renamed"] is True

    second = await (await client.post("/api/track/track-1/rename-file")).get_json()
    assert second["success"] is True
    assert second["unchanged"] is True
    assert second["renamed"] is False
    # No " (1)"-suffixed files anywhere under the music root.
    assert not [p for p in tmp_path.rglob("*") if " (1)" in p.name]


async def test_track_rename_absolute_path(app, client, tmp_path, monkeypatch):
    """Absolute file_path values keep working (legacy/downloads imports)."""
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path))
    os.makedirs(tmp_path / "Artist" / "Album")
    src_file = tmp_path / "Artist" / "Album" / "02 - Other.mp3"
    src_file.write_bytes(b"other payload")

    _seed_track(str(src_file), title="Other")

    r = await client.post("/api/track/track-1/rename-file")
    data = await r.get_json()

    assert data["success"] is True
    assert data["renamed"] is True
    assert not src_file.exists()
    moved = tmp_path / "Artist" / "2024 - Album" / "01. Artist - Other.mp3"
    assert moved.exists()
    assert moved.read_bytes() == b"other payload"


async def test_track_rename_missing_file_returns_error(app, client, tmp_path, monkeypatch):
    """A file_path that points nowhere reports an error, never a deletion."""
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path))
    _seed_track("Artist/Album/01 - Ghost.mp3")

    r = await client.post("/api/track/track-1/rename-file")
    data = await r.get_json()

    assert data["success"] is False
    assert "not found" in (data.get("error") or "").lower()
