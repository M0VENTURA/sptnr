"""Regression tests: Matched Folders must NEVER merge a band folder's albums
under /torrents, and the merge-guard must refuse operating on such folders.

Reported bug: "Matched folders are still merging all folders into the top
level folder under /torrents" — a band folder like
``/downloads/torrents/Ignea - (ex - Parallax)/`` holding one album subfolder
per year rendered as ONE entry with every album's audio merged (58 audio
files), and matching/deleting it touched the whole directory.

Fixed points pinned here:
1. ``_collect_album_folders`` surfaces each ALBUM subfolder as its own
   candidate — band folders (no direct audio) are flattened, and a folder
   with direct audio that ALSO holds album subfolders yields BOTH its own
   entry and the subfolder entries.
2. ``_iter_torrent_album_candidates`` (used by Refresh Matches) recurses to
   the same album level, so band/root associations migrate to per-album rows.
3. ``_resolve_folder_match`` inherits a BAND-level (ancestor) association for
   torrent-root descendants, not just the root row.
4. ``_assert_single_album_folder`` refuses match/associate/delete/move on the
   torrents root or any folder that merges multiple albums.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.downloads import download_folder_service as dfs


@pytest.fixture()
def match_repo_env(monkeypatch):
    """Point the folder-match repository at a fresh SQLite DB."""
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE folder_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT NOT NULL,
                release_mbid TEXT NOT NULL,
                release_title TEXT,
                artist TEXT,
                release_year INTEGER,
                status TEXT DEFAULT 'matched',
                created_at TEXT,
                updated_at TEXT,
                UNIQUE (folder_path)
            )
        """))
    sess_factory = sessionmaker(bind=engine, expire_on_commit=False)

    class _Session:
        def __init__(self, session):
            self._session = session

        def execute(self, *args, **kwargs):
            return self._session.execute(*args, **kwargs)

        def commit(self):
            self._session.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *exc):
            if exc_type is None:
                self._session.commit()
            self._session.close()
            return False

    session = sess_factory()
    monkeypatch.setattr(
        "db.repositories.folder_match_repository.db_session",
        lambda *a, **kw: _Session(session),
    )
    return engine


@pytest.fixture()
def downloads_env(tmp_path, monkeypatch):
    """Downloads tree with a torrents root holding a BAND folder that has
    multiple album subfolders (the Ignea layout)."""
    root = tmp_path / "downloads"
    root.mkdir()

    torrents = root / "Torrents"
    torrents.mkdir()
    band = torrents / "Ignea - (ex - Parallax)"
    band.mkdir()
    (band / "2013 - Sputnik (EP)").mkdir()
    (band / "2013 - Sputnik (EP)" / "01. Sputnik.mp3").write_bytes(b"x")
    (band / "2013 - Sputnik (EP)" / "03. Mind the Past.mp3").write_bytes(b"x")
    (band / "2017 - The Sign of Faith").mkdir()
    (band / "2017 - The Sign of Faith" / "02. Alexandria.mp3").write_bytes(b"x")
    (band / "2017 - The Sign of Faith" / "Scans").mkdir()
    (band / "2017 - The Sign of Faith" / "Scans" / "img001.jpg").write_bytes(b"x")
    (band / "2020 - The Realms of Fire and Death").mkdir()
    (band / "2020 - The Realms of Fire and Death" / "01. Queen Dies.mp3").write_bytes(b"x")

    # A top-level folder with direct audio + a sibling album subfolder (the
    # "stray track" case) must yield BOTH entries.
    (root / "Band With Stray").mkdir()
    (root / "Band With Stray" / "00 - Stray.mp3").write_bytes(b"x")
    (root / "Band With Stray" / "2021 - Real Album").mkdir()
    (root / "Band With Stray" / "2021 - Real Album" / "01 - A.flac").write_bytes(b"x")

    monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda *a, **kw: str(root))
    monkeypatch.setattr(dfs, "resolve_original_archive_dir", lambda: str(root / "Original"))
    monkeypatch.setattr(dfs, "_tracked_monitoring_folders", lambda: set())
    monkeypatch.setattr(dfs, "_imported_source_paths", lambda: set())
    return root


def _band_album(root: str, album: str) -> str:
    return os.path.normpath(os.path.join(root, "Torrents", "Ignea - (ex - Parallax)", album))


class TestBandFolderNeverMerged:
    def test_each_album_subfolder_is_its_own_candidate(self, downloads_env):
        """A band folder with album subfolders must yield ONE candidate per
        album — never the band folder itself (the merged-entry bug)."""
        result = dfs.get_unmatched_folders()
        assert result["success"] is True

        names = [f["name"] for f in result["folders"]]
        for album in ("2013 - Sputnik (EP)", "2017 - The Sign of Faith", "2020 - The Realms of Fire and Death"):
            assert any(n.endswith(os.path.join("Ignea - (ex - Parallax)", album)) for n in names), album

        # The band folder itself must NOT appear as a candidate.
        assert not any(n.endswith(os.path.join("Torrents", "Ignea - (ex - Parallax)")) for n in names)

    def test_album_audio_counts_are_scoped(self, downloads_env):
        """Each album entry must only count ITS OWN audio files."""
        result = dfs.get_unmatched_folders()
        by_name = {os.path.basename(f["name"]): f for f in result["folders"]}
        assert by_name["2013 - Sputnik (EP)"]["audio_count"] == 2
        assert by_name["2017 - The Sign of Faith"]["audio_count"] == 1
        assert by_name["2020 - The Realms of Fire and Death"]["audio_count"] == 1

    def test_stray_track_folder_yields_both_entries(self, downloads_env):
        """A top-level folder with direct audio AND an album subfolder must
        produce BOTH its own entry (the stray track) and the subfolder's."""
        result = dfs.get_unmatched_folders()
        names = [f["name"] for f in result["folders"]]
        assert any(n.endswith(os.path.join("Band With Stray")) for n in names)
        assert any(n.endswith(os.path.join("Band With Stray", "2021 - Real Album")) for n in names)


class TestBandLevelAssociationInherited:
    def test_albums_inherit_band_level_match(self, downloads_env, match_repo_env):
        """A folder_matches row pointing at the BAND folder (not the torrents
        root) must make every album subfolder show as matched."""
        from db.repositories.folder_match_repository import upsert_folder_match

        band = os.path.normpath(os.path.join(str(downloads_env), "Torrents", "Ignea - (ex - Parallax)"))
        upsert_folder_match(
            folder_path=band,
            release_mbid="11111111-2222-3333-4444-555555555555",
            release_title="The Band Album",
            artist="Ignea",
            release_year=2024,
            status="matched",
        )

        result = dfs.get_unmatched_folders()
        by_name = {os.path.basename(f["name"]): f for f in result["folders"]}
        for album in ("2013 - Sputnik (EP)", "2017 - The Sign of Faith"):
            entry = by_name[album]
            assert entry["status"] == "matched", album
            assert entry["release_mbid"] == "11111111-2222-3333-4444-555555555555", album


class TestMergeGuard:
    def test_torrents_root_refused_for_delete(self, downloads_env):
        """Deleting the torrents root itself must be refused."""
        root = str(downloads_env)
        result = dfs.delete_download_folder(os.path.join(root, "Torrents"))
        assert result["success"] is False
        assert "torrents root" in (result.get("error") or "").lower()
        assert os.path.isdir(os.path.join(root, "Torrents"))

    def test_band_folder_refused_for_delete(self, downloads_env):
        """Deleting a band folder that holds album subfolders must be refused
        (it would merge/delete multiple albums)."""
        band = os.path.normpath(os.path.join(str(downloads_env), "Torrents", "Ignea - (ex - Parallax)"))
        result = dfs.delete_download_folder(band)
        assert result["success"] is False
        assert "multiple album" in (result.get("error") or "").lower()
        assert os.path.isdir(band)

    def test_album_folder_still_deletable(self, downloads_env):
        """A real album folder (no album subfolders) is still a safe delete
        target — the guard must not over-block."""
        album = _band_album(str(downloads_env), "2013 - Sputnik (EP)")
        result = dfs.delete_download_folder(album)
        assert result["success"] is True
        assert not os.path.isdir(album)

    def test_associate_refuses_band_folder(self, downloads_env, monkeypatch):
        """associate_folder_to_release must refuse a band folder before any
        MusicBrainz call."""
        called = {}

        def _fake_resolve_release(client, mb_id):
            called["resolved"] = True
            return None, ""

        monkeypatch.setattr(dfs, "_resolve_release", _fake_resolve_release)

        band = os.path.normpath(os.path.join(str(downloads_env), "Torrents", "Ignea - (ex - Parallax)"))
        result = dfs.associate_folder_to_release(band, "11111111-2222-3333-4444-555555555555")
        assert result["success"] is False
        assert "multiple album" in (result.get("error") or "").lower()
        assert not called.get("resolved")  # guard fired before MB lookup


class TestRefreshMigratesBandLevelRows:
    def test_band_row_migrates_to_albums(self, downloads_env, match_repo_env):
        """refresh_folder_matches must migrate a BAND-level row down to one
        row per album subfolder (not just torrents-root rows)."""
        from db.repositories.folder_match_repository import (
            get_all_folder_matches,
            upsert_folder_match,
        )

        band = os.path.normpath(os.path.join(str(downloads_env), "Torrents", "Ignea - (ex - Parallax)"))
        upsert_folder_match(
            folder_path=band,
            release_mbid="11111111-2222-3333-4444-555555555555",
            release_title="The Band Album",
            artist="Ignea",
            release_year=2024,
            status="matched",
        )

        result = dfs.refresh_folder_matches()
        assert result["success"] is True
        assert result["updated"] == 3  # one row per album subfolder

        remaining = {os.path.normpath(m["folder_path"]): m for m in get_all_folder_matches()}
        assert band not in remaining  # band row migrated away
        for album in ("2013 - Sputnik (EP)", "2017 - The Sign of Faith", "2020 - The Realms of Fire and Death"):
            album_row = remaining.get(_band_album(str(downloads_env), album))
            assert album_row is not None, album
            assert album_row["release_mbid"] == "11111111-2222-3333-4444-555555555555", album
