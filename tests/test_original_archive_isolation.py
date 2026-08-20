"""Tests for the /Original archive isolation + completed-download re-download guard.

The FLAC conversion archive (downloads/<original_subfolder>, default
``Original``) must NEVER be seen by the queue: its files were already
imported (converted), and re-discovering/re-queuing them would download the
album AGAIN as duplicates.

Fixes pinned here:
1. ``_get_files_in_folder`` prunes the archive subfolder at ANY depth (a
   nested ``<album>/Original/`` must not surface archived FLACs).
2. ``discover_audio_files`` prunes the archive by name at any depth.
3. ``_wait_for_transfer_file`` checks the archive when the slskd
   ``localFilePath`` no longer exists (a converted+archived original is
   proof the download WAS imported — without this the completion service
   marked the item failed and the retry scheduler re-downloaded it).
"""

from __future__ import annotations

import os
import tempfile


class TestGetFilesInFolderPrunesArchive:
    def test_nested_original_folder_excluded(self, monkeypatch):
        """An ``Original`` subfolder inside a tracked folder must not surface."""
        from services.infrastructure import filesystem_service as fsvc

        root = tempfile.mkdtemp()
        try:
            # downloads/Artist Album/
            album = os.path.join(root, "Artist Album")
            os.makedirs(os.path.join(album, "Original", "Artist", "Album"), exist_ok=True)
            # Archived original (should be excluded)
            open(os.path.join(album, "Original", "Artist", "Album", "01 Song.flac"), "w").close()
            # Pending download (should be included)
            open(os.path.join(album, "02 Song.mp3"), "w").close()

            monkeypatch.setattr(fsvc, "_original_archive_subfolder_name", lambda: "Original")
            files = fsvc._get_files_in_folder(album)
            names = [f["name"] for f in files]
            assert any("02 Song.mp3" in n for n in names)
            assert not any("01 Song.flac" in n for n in names)
            assert not any("Original" in n for n in names)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class TestDiscoverAudioFilesPrunesArchive:
    def test_archive_dir_excluded_even_nested(self, monkeypatch):
        from services.downloads import download_scan_service as dss

        root = tempfile.mkdtemp()
        try:
            downloads = os.path.join(root, "downloads")
            os.makedirs(os.path.join(downloads, "Artist Album", "Original"), exist_ok=True)
            os.makedirs(os.path.join(downloads, "Artist Album"), exist_ok=True)
            archived = os.path.join(downloads, "Artist Album", "Original", "01 Song.flac")
            pending = os.path.join(downloads, "Artist Album", "02 Song.mp3")
            open(archived, "w").close()
            open(pending, "w").close()

            monkeypatch.setattr(dss, "resolve_downloads_dir", lambda **k: downloads)
            monkeypatch.setattr(dss, "resolve_original_archive_dir", lambda: os.path.join(downloads, "Original"))
            monkeypatch.setattr("services.infrastructure.filesystem_service._original_archive_subfolder_name", lambda: "Original")

            found = dss.discover_audio_files()
            paths = [f.full_path for f in found]
            assert pending in paths
            assert archived not in paths
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class TestWaitForTransferFileChecksArchive:
    def test_archived_original_counts_as_landed(self, monkeypatch):
        """A file archived under Original after conversion proves import."""
        from services.downloads import download_completion_service as dcs

        root = tempfile.mkdtemp()
        try:
            archive = os.path.join(root, "Original")
            os.makedirs(os.path.join(archive, "Artist", "Album"), exist_ok=True)
            archived = os.path.join(archive, "Artist", "Album", "01 Song.flac")
            open(archived, "w").close()

            monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: os.path.join(root, "downloads"))
            monkeypatch.setattr(
                "services.infrastructure.filesystem_service.resolve_original_archive_dir",
                lambda: archive,
            )
            # The slskd localFilePath and the monitored dir BOTH lack the file
            # (it was moved), but the archive has it.
            result = dcs._wait_for_transfer_file(
                "01 Song.flac",
                os.path.join(root, "downloads", "01 Song.flac"),
            )
            assert result == archived
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_no_archive_no_file_returns_none(self, monkeypatch):
        from services.downloads import download_completion_service as dcs

        root = tempfile.mkdtemp()
        try:
            archive = os.path.join(root, "Original")
            os.makedirs(archive, exist_ok=True)
            monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: os.path.join(root, "downloads"))
            monkeypatch.setattr(
                "services.infrastructure.filesystem_service.resolve_original_archive_dir",
                lambda: archive,
            )
            assert dcs._wait_for_transfer_file("01 Song.flac", "") is None
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
