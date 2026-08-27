"""Regression tests: mismatched Soulseek downloads must be deleted and the
peer blocked so a retry does not re-download the same wrong file.

Covers:
- ``_block_peer_for_queue_item`` finds the offending transfer (by remote
  filename / basename) and blocks that peer+filename.
- ``_select_best_result``'s acceptance threshold rejects weak matches that
  would later fail the completion check (was 30.0, now 45.0).
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

from services.downloads.download_completion_service import (
    _block_peer_for_queue_item,
    _delete_mismatched_download,
)


class TestDeleteMismatchedDownload:
    def test_deletes_existing_file(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
            path = fh.name
        try:
            assert os.path.isfile(path)
            _delete_mismatched_download(path, queue_id=1, reason="test")
            assert not os.path.isfile(path)
        finally:
            if os.path.isfile(path):
                os.remove(path)

    def test_missing_file_is_noop(self):
        # Deleting a non-existent path must not raise.
        _delete_mismatched_download("/tmp/definitely-not-here-xyz.mp3", queue_id=1, reason="test")


class TestBlockPeerForQueueItem:
    """The peer that supplied a mismatched file must be blocked so the retry
    search drops it instead of re-downloading the same wrong file."""

    def _transfer(self, username: str, filename: str, local: str) -> dict:
        return {
            "username": username,
            "filename": filename,
            "localFilePath": local,
        }

    def test_blocks_peer_by_remote_filename(self):
        remote = "SomeUser/BadArtist - WrongAlbum - 03 WrongTrack.flac"
        local = "/downloads/BadArtist - WrongAlbum - 03 WrongTrack.flac"
        fake_client = MagicMock()
        fake_client.enabled = True
        fake_slskd = MagicMock()
        fake_slskd.get_completed_transfers.return_value = [
            self._transfer("wrong_peer", remote, local),
            self._transfer("good_peer", "Other - Fine.flac", "/downloads/Other - Fine.flac"),
        ]
        blocked = {}

        def _fake_block_peer(username, filename):
            blocked[(str(username), str(filename))] = True

        with patch("api_clients.slskd_http.get_slskd_client", return_value=fake_client), \
             patch("services.downloads.slskd_service.SlskdService", return_value=fake_slskd), \
             patch("services.downloads.download_pipeline_service._block_peer", side_effect=_fake_block_peer):
            _block_peer_for_queue_item(queue_id=42, found_filename=remote)

        assert ("wrong_peer", remote) in blocked
        assert ("good_peer", "Other - Fine.flac") not in blocked

    def test_blocks_peer_by_basename_match(self):
        remote = "userX/random-dir/01 Real Track.flac"
        # found_filename stores only the basename on some paths.
        fake_client = MagicMock()
        fake_client.enabled = True
        fake_slskd = MagicMock()
        fake_slskd.get_completed_transfers.return_value = [
            self._transfer("peer_y", "userX/random-dir/01 Real Track.flac", "/dl/01 Real Track.flac"),
        ]
        blocked = {}

        def _fake_block_peer(username, filename):
            blocked[(str(username), str(filename))] = True

        with patch("api_clients.slskd_http.get_slskd_client", return_value=fake_client), \
             patch("services.downloads.slskd_service.SlskdService", return_value=fake_slskd), \
             patch("services.downloads.download_pipeline_service._block_peer", side_effect=_fake_block_peer):
            _block_peer_for_queue_item(queue_id=7, found_filename="01 Real Track.flac")

        assert ("peer_y", "userX/random-dir/01 Real Track.flac") in blocked


class TestSelectBestResultThreshold:
    """The pipeline acceptance threshold must reject matches that the
    completion check would later reject (< 0.45 on the 0-1 scale)."""

    def test_weak_title_only_candidate_rejected(self):
        from services.downloads.download_pipeline_service import _select_best_result

        # Artist matches but title is a weak substring — scores ~30-40 on the
        # pipeline scale, which the old 30.0 floor accepted but the completion
        # check (0.45 / ~53 pipeline-equivalent) rejects.
        weak = {
            "filename": "Radiohead - OK Computer - 01 Airbag.mp3",
            "bitrate": 320,
            "has_free_upload_slot": True,
            "queue_length": 0,
            "upload_speed": 2_000_000,
        }
        best = _select_best_result(
            [weak],
            expected_artist="Radiohead",
            expected_title="Subterranean Homesick Alien",
            expected_album="OK Computer",
            expected_duration=268,
        )
        # The title "Subterranean Homesick Alien" shares no token with
        # "Airbag" — the hard title gate rejects it outright (0.0).
        assert best is None

    def test_strong_candidate_still_selected(self):
        from services.downloads.download_pipeline_service import _select_best_result

        strong = {
            "filename": "Radiohead - OK Computer - 02 Subterranean Homesick Alien.flac",
            "bitrate": 320,
            "has_free_upload_slot": True,
            "queue_length": 0,
            "upload_speed": 2_000_000,
        }
        best = _select_best_result(
            [strong],
            expected_artist="Radiohead",
            expected_title="Subterranean Homesick Alien",
            expected_album="OK Computer",
            expected_duration=268,
        )
        assert best is not None
        assert best["filename"] == strong["filename"]
