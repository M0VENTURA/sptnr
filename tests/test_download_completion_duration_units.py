"""Regression tests for download-completion duration unit handling.

Reported bug: "queue items are still failing to match when downloaded".

Root cause: ``download_queue.duration`` is written in inconsistent units.
Rows created through ``add_release_tracks_to_queue`` (the MusicBrainz "Download
All Tracks" flow) persist the raw MusicBrainz millisecond ``length`` (e.g.
``233000`` for 3:53), while rows created through the download matching service
persist plain seconds. The post-download matchers compared the stored duration
against the file duration in *seconds*, so any queue item carrying a millisecond
duration hard-rejected every candidate file:

- ``_metadata_matches_queue_item`` returned ``False`` (strong reject) because
  ``abs(file_sec - queue_duration)`` was ~232,000s > the 30s limit.
- ``check_completed_downloads``' pre-copy check marked the item failed with
  "duration mismatch" because ``abs(expected_dur - actual_dur) > 20``.

Because the metadata reject caused the fuzzy filename matcher to ``continue``
past every candidate, an entire MusicBrainz album (e.g. Spice Girls' "Greatest
Hits", 28 tracks) failed even though every file was present on disk.

Fix: ``queue_duration_seconds`` normalises a stored duration to seconds
(>= 3000 is treated as milliseconds), and all consumers use it.
"""

from __future__ import annotations

import os

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC as MFLAC

from helpers.normalization_service import queue_duration_seconds
from services.queue.queue_metadata_matcher import _metadata_matches_queue_item


def _write_flac(name: str, tags: dict, duration_seconds: int = 233) -> str:
    path = os.path.join("/tmp", name)
    sr = 44100
    data = np.zeros(int(sr * duration_seconds), dtype=np.float32)
    sf.write(path, data, sr, format="FLAC")
    audio = MFLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


def _queue_item(duration) -> dict:
    return {
        "title": "Too Much - Radio Edit",
        "artist": "Spice Girls",
        "album": "Greatest Hits",
        "album_artist": "Spice Girls",
        "track_number": "8",
        "duration": duration,
    }


class TestQueueDurationSeconds:
    def test_milliseconds_are_converted(self):
        assert queue_duration_seconds(233000) == 233.0

    def test_seconds_are_passed_through(self):
        assert queue_duration_seconds(233) == 233.0

    def test_missing_returns_none(self):
        assert queue_duration_seconds(None) is None
        assert queue_duration_seconds("") is None

    def test_non_numeric_returns_none(self):
        assert queue_duration_seconds("low") is None
        assert queue_duration_seconds("abc") is None

    def test_zero_returns_none(self):
        assert queue_duration_seconds(0) is None


class TestMetadataMatcherDurationUnits:
    """A matching file must NOT be hard-rejected because the stored queue
    duration is in milliseconds rather than seconds."""

    def test_matching_file_with_ms_duration_is_accepted(self):
        path = _write_flac(
            "dur_units_ms.flac",
            {"title": "Too Much - Radio Edit", "artist": "Spice Girls"},
        )
        try:
            assert _metadata_matches_queue_item(path, _queue_item(233000)) is True
        finally:
            os.remove(path)

    def test_matching_file_with_seconds_duration_is_accepted(self):
        path = _write_flac(
            "dur_units_sec.flac",
            {"title": "Too Much - Radio Edit", "artist": "Spice Girls"},
        )
        try:
            assert _metadata_matches_queue_item(path, _queue_item(233)) is True
        finally:
            os.remove(path)

    def test_file_without_duration_still_matches(self):
        path = _write_flac(
            "dur_units_none.flac",
            {"title": "Too Much - Radio Edit", "artist": "Spice Girls"},
        )
        try:
            assert _metadata_matches_queue_item(path, _queue_item(None)) is True
        finally:
            os.remove(path)

    def test_genuine_duration_mismatch_still_rejects(self):
        # A genuinely different version (~4 min vs 3:53 expected) must keep
        # rejecting — the fix only aligns units, it must not disable the guard.
        path = _write_flac(
            "dur_units_wrong.flac",
            {"title": "Too Much - Radio Edit", "artist": "Spice Girls"},
            duration_seconds=280,
        )
        try:
            assert _metadata_matches_queue_item(path, _queue_item(233000)) is False
        finally:
            os.remove(path)
