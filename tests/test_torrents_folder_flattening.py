"""Tests for the Matched-Folders torrents-root flattening.

The monitor page's Matched Folders section previously treated the torrents
root as a single folder: a case-sensitive ``entry == "torrents"`` skip let
``Torrents``/``TORRENTS`` through, and ``_get_files_in_folder`` (recursive,
depth 3) then merged EVERY album subfolder into one Matched Folder whose
``group_key`` was the root name — so matching that entry matched the whole
``/Torrents`` directory instead of a single album.

The fix flattens the torrents root (any casing) into one Matched Folder per
album subfolder, so each album gets its own Match / Confirm / Delete /
per-track actions.
"""

from __future__ import annotations

import os

import pytest

from services.downloads import download_folder_service as dfs


@pytest.fixture()
def downloads_env(tmp_path, monkeypatch):
    """Build a downloads tree with a torrents root holding two albums."""
    root = tmp_path / "downloads"
    root.mkdir()

    # Normal top-level folder.
    (root / "Direct Album").mkdir()
    (root / "Direct Album" / "01 - Song.flac").write_bytes(b"x")

    # Torrents root with two album subfolders (capital T — the case the old
    # skip missed).
    torrents = root / "Torrents"
    torrents.mkdir()
    (torrents / "Album One").mkdir()
    (torrents / "Album One" / "01 - A.flac").write_bytes(b"x")
    (torrents / "Album One" / "02 - B.flac").write_bytes(b"x")
    (torrents / "Album Two").mkdir()
    (torrents / "Album Two" / "01 - C.flac").write_bytes(b"x")

    # Hidden + archive dirs must stay hidden.
    (root / ".hidden").mkdir()
    (root / "__trash").mkdir()

    monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda *a, **kw: str(root))
    monkeypatch.setattr(dfs, "resolve_original_archive_dir", lambda: str(root / "Original"))
    # No DB rows for the two-phase-match merge or imported paths.
    monkeypatch.setattr(dfs, "_tracked_monitoring_folders", lambda: set())
    monkeypatch.setattr(dfs, "_imported_source_paths", lambda: set())
    return root


class TestTorrentsRootFlattening:
    def test_torrents_albums_appear_as_individual_folders(self, downloads_env):
        """Each album under /Torrents must be its OWN Matched Folder entry —
        never one merged 'Torrents' folder."""
        result = dfs.get_unmatched_folders()
        assert result["success"] is True

        names = [f["name"] for f in result["folders"]]
        # The two torrent album subfolders are present as separate entries.
        assert any(n.endswith(os.path.join("Torrents", "Album One")) for n in names)
        assert any(n.endswith(os.path.join("Torrents", "Album Two")) for n in names)
        # The direct top-level folder is still there.
        assert any(n.endswith(os.path.join("downloads", "Direct Album")) for n in names)

        # NO entry is the torrents root itself (the merged-folder bug).
        assert not any(n.endswith(os.path.join("downloads", "Torrents")) for n in names)

    def test_torrents_albums_are_not_merged_by_group_key(self, downloads_env):
        """The album entries must NOT share a single 'Torrents' group_key —
        that was the visible merge symptom."""
        result = dfs.get_unmatched_folders()
        album_entries = [
            f for f in result["folders"]
            if os.path.join("Torrents") in str(f["name"]).replace("\\", "/")
        ]
        # Each album has its own group_key (folder-path fallback when no
        # embedded metadata is present).
        keys = {f["group_key"] for f in album_entries}
        assert len(keys) == 2
        assert all("Torrents" not in k for k in keys)

    def test_torrents_albums_audio_counts_are_scoped(self, downloads_env):
        """Album One has 2 audio files, Album Two has 1 — the recursion must
        not pull the sibling album's files into either entry."""
        result = dfs.get_unmatched_folders()
        by_name = {os.path.basename(f["name"]): f for f in result["folders"]}
        assert by_name["Album One"]["audio_count"] == 2
        assert by_name["Album Two"]["audio_count"] == 1

    def test_hidden_and_archive_dirs_still_hidden(self, downloads_env):
        """The flattening must keep hiding dot/dunder dirs and the archive."""
        result = dfs.get_unmatched_folders()
        names = [os.path.basename(f["name"]) for f in result["folders"]]
        assert ".hidden" not in names
        assert "__trash" not in names
        assert "Original" not in names


class TestTorrentsRootLowercase:
    def test_lowercase_torrents_albums_appear(self, tmp_path, monkeypatch):
        """Lowercase 'torrents' (the previously-skipped case) must now also
        surface its album subfolders as individual entries."""
        root = tmp_path / "downloads"
        root.mkdir()
        (root / "torrents").mkdir()
        (root / "torrents" / "Album").mkdir()
        (root / "torrents" / "Album" / "01 - X.flac").write_bytes(b"x")

        monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda *a, **kw: str(root))
        monkeypatch.setattr(dfs, "resolve_original_archive_dir", lambda: str(root / "Original"))
        monkeypatch.setattr(dfs, "_tracked_monitoring_folders", lambda: set())
        monkeypatch.setattr(dfs, "_imported_source_paths", lambda: set())

        result = dfs.get_unmatched_folders()
        names = [f["name"] for f in result["folders"]]
        assert any(n.endswith(os.path.join("torrents", "Album")) for n in names)
        # The torrents root itself is not an entry.
        assert not any(n.endswith(os.path.join("downloads", "torrents")) for n in names)
