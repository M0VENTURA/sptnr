"""Tests for torrent-root match propagation + the Refresh Matches endpoint.

The Matched-Folders torrents-root flattening turned the single merged
``/Torrents`` entry into one entry per album subfolder.  Associations
recorded BEFORE the flattening still point at the root.  These tests cover:

1. ``_resolve_folder_match`` — a root-level association is inherited by each
   album subfolder (the folder shows as matched with the two-state actions).
2. ``refresh_folder_matches`` — the root-level row is migrated down into one
   row per album subfolder and the root row is deleted.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.downloads import download_folder_service as dfs

_RELEASE_MBID = "11111111-2222-3333-4444-555555555555"


def _make_folder_matches_engine():
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
    return engine


@pytest.fixture()
def match_repo_env(monkeypatch):
    """Point the folder-match repository at a fresh SQLite DB."""
    engine = _make_folder_matches_engine()
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
    """Build a downloads tree with a torrents root holding two albums."""
    root = tmp_path / "downloads"
    root.mkdir()

    torrents = root / "Torrents"
    torrents.mkdir()
    (torrents / "Album One").mkdir()
    (torrents / "Album One" / "01 - A.flac").write_bytes(b"x")
    (torrents / "Album Two").mkdir()
    (torrents / "Album Two" / "01 - B.flac").write_bytes(b"x")

    monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda *a, **kw: str(root))
    monkeypatch.setattr(dfs, "resolve_original_archive_dir", lambda: str(root / "Original"))
    monkeypatch.setattr(dfs, "_tracked_monitoring_folders", lambda: set())
    monkeypatch.setattr(dfs, "_imported_source_paths", lambda: set())
    return root


def _album_path(root: str, name: str) -> str:
    return os.path.normpath(os.path.join(root, "Torrents", name))


class TestRootMatchPropagation:
    def test_albums_inherit_root_association(self, downloads_env, match_repo_env):
        """A folder_matches row pointing at the torrent root must make every
        album subfolder show as matched with the stored release."""
        from db.repositories.folder_match_repository import upsert_folder_match

        root = str(downloads_env)
        torrents_root = os.path.normpath(os.path.join(root, "Torrents"))
        upsert_folder_match(
            folder_path=torrents_root,
            release_mbid=_RELEASE_MBID,
            release_title="The Album",
            artist="The Artist",
            release_year=2024,
            status="matched",
        )

        result = dfs.get_unmatched_folders()
        assert result["success"] is True
        by_name = {os.path.basename(f["name"]): f for f in result["folders"]}

        for album in ("Album One", "Album Two"):
            entry = by_name[album]
            assert entry["status"] == "matched", album
            assert entry["release_mbid"] == _RELEASE_MBID, album
            assert entry["match"] is not None, album
            assert entry["match"]["release_mbid"] == _RELEASE_MBID, album

    def test_own_association_wins_over_root(self, downloads_env, match_repo_env):
        """A subfolder with its OWN association must not be overwritten by
        the root fallback."""
        from db.repositories.folder_match_repository import upsert_folder_match

        root = str(downloads_env)
        torrents_root = os.path.normpath(os.path.join(root, "Torrents"))
        album_one = _album_path(root, "Album One")
        own_mbid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        upsert_folder_match(folder_path=torrents_root, release_mbid=_RELEASE_MBID, status="matched")
        upsert_folder_match(folder_path=album_one, release_mbid=own_mbid, status="matched")

        result = dfs.get_unmatched_folders()
        by_name = {os.path.basename(f["name"]): f for f in result["folders"]}

        assert by_name["Album One"]["release_mbid"] == own_mbid
        assert by_name["Album Two"]["release_mbid"] == _RELEASE_MBID

    def test_no_root_association_stays_unmatched(self, downloads_env, match_repo_env):
        """Without any stored association the albums remain unmatched."""
        result = dfs.get_unmatched_folders()
        by_name = {os.path.basename(f["name"]): f for f in result["folders"]}
        assert by_name["Album One"]["status"] == "unmatched"
        assert by_name["Album Two"]["status"] == "unmatched"


class TestRefreshFolderMatches:
    def test_root_match_migrates_to_albums(self, downloads_env, match_repo_env):
        """refresh_folder_matches replaces the root row with one row per
        album subfolder and deletes the root row."""
        from db.repositories.folder_match_repository import (
            upsert_folder_match,
            get_all_folder_matches,
        )

        root = str(downloads_env)
        torrents_root = os.path.normpath(os.path.join(root, "Torrents"))
        upsert_folder_match(
            folder_path=torrents_root,
            release_mbid=_RELEASE_MBID,
            release_title="The Album",
            artist="The Artist",
            release_year=2024,
            status="matched",
        )

        result = dfs.refresh_folder_matches()
        assert result["success"] is True
        assert result["updated"] == 2

        remaining = {os.path.normpath(m["folder_path"]): m for m in get_all_folder_matches()}
        assert torrents_root not in remaining
        assert remaining[_album_path(root, "Album One")]["release_mbid"] == _RELEASE_MBID
        assert remaining[_album_path(root, "Album Two")]["release_mbid"] == _RELEASE_MBID
        assert remaining[_album_path(root, "Album One")]["release_title"] == "The Album"

    def test_idempotent_when_no_root_matches(self, downloads_env, match_repo_env):
        """A second refresh (or a run with no root-level rows) changes nothing."""
        from db.repositories.folder_match_repository import (
            upsert_folder_match,
            get_all_folder_matches,
        )

        root = str(downloads_env)
        album_one = _album_path(root, "Album One")
        upsert_folder_match(folder_path=album_one, release_mbid=_RELEASE_MBID, status="matched")

        result = dfs.refresh_folder_matches()
        assert result["success"] is True
        assert result["updated"] == 0

        remaining = {os.path.normpath(m["folder_path"]): m for m in get_all_folder_matches()}
        assert remaining.get(album_one) is not None
        assert os.path.normpath(os.path.join(root, "Torrents")) not in remaining

    def test_lowercase_torrents_root_refreshed(self, tmp_path, monkeypatch, match_repo_env):
        """Lowercase 'torrents' root associations are migrated too."""
        root = tmp_path / "downloads"
        root.mkdir()
        (root / "torrents").mkdir()
        (root / "torrents" / "Album").mkdir()
        (root / "torrents" / "Album" / "01 - X.flac").write_bytes(b"x")

        monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda *a, **kw: str(root))
        monkeypatch.setattr(dfs, "resolve_original_archive_dir", lambda: str(root / "Original"))
        monkeypatch.setattr(dfs, "_tracked_monitoring_folders", lambda: set())
        monkeypatch.setattr(dfs, "_imported_source_paths", lambda: set())

        from db.repositories.folder_match_repository import (
            upsert_folder_match,
            get_all_folder_matches,
        )

        torrents_root = os.path.normpath(str(root / "torrents"))
        upsert_folder_match(folder_path=torrents_root, release_mbid=_RELEASE_MBID, status="matched")

        result = dfs.refresh_folder_matches()
        assert result["success"] is True
        assert result["updated"] == 1

        remaining = {os.path.normpath(m["folder_path"]): m for m in get_all_folder_matches()}
        assert torrents_root not in remaining
        assert remaining[os.path.normpath(str(root / "torrents" / "Album"))]["release_mbid"] == _RELEASE_MBID
