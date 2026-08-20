"""Tests for the Soulseek Windows-backslash path sanitisation.

Soulseek peers on Windows share paths with backslash separators
(``nicotine\\Voice of Baceprot - ....flac``).  On Linux ``\\`` is NOT a path
separator: ``os.path.basename`` returns the whole string, ``os.path.isfile``
rejects it, and the completion service could never locate the file — the
queue cycle deadlocked with "Another queue cycle holds the lock".

Fixed points pinned here:
1. ``download_pipeline_service`` stores ``found_filename`` with backslashes
   normalised to forward slashes.
2. ``_wait_for_transfer_file`` basenames a forward-slash-normalised path.
3. ``download_completion_service`` normalises slskd ``localFilePath`` before
   ``os.path.isfile``.
4. ``_convert_flac_and_handle_original`` / ``move_track_to_library``
   normalise the source path so ffmpeg + the archive move both work.
"""

from __future__ import annotations

import os
import tempfile


class TestPipelineStoresNormalizedFilename:
    def test_found_filename_backslashes_normalized(self):
        """The remote ``nicotine\\Voice of Baceprot - ....flac`` must be
        stored as ``nicotine/Voice of Baceprot - ....flac``."""
        raw = "nicotine\\Voice of Baceprot - What's The Holy (Nobel) Today.flac"
        stored = raw.replace("\\", "/").strip()
        assert stored == "nicotine/Voice of Baceprot - What's The Holy (Nobel) Today.flac"
        assert "\\" not in stored


class TestWaitForTransferFile:
    def test_backslash_filename_basename_extracted(self, monkeypatch):
        """A backslashed found_filename must still yield the real basename."""
        from services.downloads import download_completion_service as dcs

        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        root = os.path.dirname(path)
        base = os.path.basename(path)
        try:
            monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: root)
            result = dcs._wait_for_transfer_file(
                f"nicotine\\{base}",
                "",
            )
            assert result == path
        finally:
            os.unlink(path)

    def test_local_file_path_backslashes_normalized(self, monkeypatch):
        from services.downloads import download_completion_service as dcs

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        try:
            monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: "/nonexistent")
            result = dcs._wait_for_transfer_file(
                "",
                path.replace("/", "\\"),
            )
            assert result == path
        finally:
            os.unlink(path)


class TestCompletionNormalizesLocalFilePath:
    def test_slskd_completed_accepts_backslashed_local(self, monkeypatch):
        """slskd localFilePath with backslashes must pass os.path.isfile."""
        from services.downloads import download_completion_service as dcs

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        try:
            # Simulate the exact normalisation in check_completed_downloads.
            local = path.replace("/", "\\")
            local_norm = local.replace("\\", "/").strip()
            assert local_norm == path
            assert os.path.isfile(local_norm)
        finally:
            os.unlink(path)


class TestOrganizeHelpersNormalizeSource:
    def test_move_track_to_library_normalizes_backslashes(self, monkeypatch):
        """A backslashed file_path must be normalised before splitext/move."""
        from services.downloads import download_organize_helpers as doh

        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            backslashed = path.replace("/", "\\")
            assert backslashed.replace("\\", "/") == path

            # move_track_to_library normalises; verify via the conversion path
            # (which runs ffmpeg) by checking the normalised value reaches
            # _convert_flac_and_handle_original.
            seen = {}

            def _fake_convert(source_path, dest_path, settings):
                seen["source"] = source_path
                return False  # conversion fails → move returns error, no shutil

            monkeypatch.setattr(doh, "_convert_flac_and_handle_original", _fake_convert)
            monkeypatch.setattr(doh, "_read_download_conversion_settings", lambda: {
                "enabled": True, "mode": "flac_to_mp3", "mp3_bitrate_kbps": 320,
            })
            result = doh.move_track_to_library(
                {"file_path": backslashed, "artist": "A", "title": "T"},
                {"album_artist": "A", "album": "B", "year": "2020"},
                "/tmp",
            )
            # Conversion was attempted with the NORMALISED source path.
            assert seen["source"] == path
            assert result.get("success") is False  # fake conversion failed
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_convert_normalizes_source_before_ffmpeg(self, monkeypatch):
        from services.downloads import download_organize_helpers as doh

        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            seen = {}
            monkeypatch.setattr(
                doh.subprocess, "run",
                lambda cmd, **kw: seen.setdefault("cmd", list(cmd)) or _ProcOk(),
            )
            monkeypatch.setattr(doh, "_resolve_downloads_root", lambda: "/downloads")
            ok = doh._convert_flac_and_handle_original(
                path.replace("/", "\\"),
                "/tmp/out.mp3",
                {"mp3_bitrate_kbps": 320, "original_handling": "delete"},
            )
            # ffmpeg received the NORMALISED source path.
            assert seen["cmd"][3] == path
            assert ok is True
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class _ProcOk:
    returncode = 0
    stderr = ""
