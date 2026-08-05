"""Tests for forced-artist-scan candidate loading and stale-track cleanup.

Covers the "forced artist scans finishing early" regressions:
- An artist-filtered scan resolves the requested name to the stored library
  name even when casing/punctuation differs ("dArtagnan" vs "D'Artagnan").
- Stale-track cleanup never wipes an artist's local library when Navidrome
  returned no track IDs (fetch failure / empty response).
"""

from __future__ import annotations

import pytest


class TestArtistFilterResolution:
    """Artist-filter resolution in services.popularity.stages.load_stage."""

    def test_exact_case_insensitive_match(self):
        from services.popularity.stages.load_stage import _resolve_artist_for_scan

        assert _resolve_artist_for_scan(["Muse", "Radiohead"], "muse") == "Muse"

    def test_punctuation_variant_resolves_to_stored_name(self):
        from services.popularity.stages.load_stage import _resolve_artist_for_scan

        # Library stores the apostrophe form; scan requested without it.
        assert _resolve_artist_for_scan(["D'Artagnan"], "dArtagnan") == "D'Artagnan"
        assert _resolve_artist_for_scan(["D'Artagnan"], "d'Artagnan") == "D'Artagnan"
        assert _resolve_artist_for_scan(["D'Artagnan"], "D ARTAGNAN") == "D'Artagnan"

    def test_no_over_matching(self):
        from services.popularity.stages.load_stage import _resolve_artist_for_scan

        # A sub-name must not resolve to a different artist.
        assert _resolve_artist_for_scan(["The Cure"], "Cure") is None
        assert _resolve_artist_for_scan(["Muse"], "Nirvana") is None

    def test_empty_filter_returns_none(self):
        from services.popularity.stages.load_stage import _resolve_artist_for_scan

        assert _resolve_artist_for_scan(["Muse"], "") is None
        assert _resolve_artist_for_scan([], "Muse") is None


class TestStaleTrackCleanupGuard:
    """Safety guard in services.scanning.cleanup."""

    def test_cleanup_skipped_when_no_navidrome_ids(self, monkeypatch):
        from services.scanning import cleanup as cleanup_mod

        deleted = []

        def fake_delete(track_ids, context=None):
            deleted.extend(track_ids)
            return len(track_ids)

        monkeypatch.setattr(cleanup_mod, "delete_tracks_by_id", fake_delete)
        monkeypatch.setattr(cleanup_mod, "log_unified", lambda *a, **k: None)

        cleanup_mod.cleanup_stale_artist_tracks_if_needed(
            artist_name="dArtagnan",
            existing_track_ids={"a", "b", "c"},
            navidrome_track_ids=set(),
        )

        assert deleted == []

    def test_cleanup_removes_only_stale(self, monkeypatch):
        from services.scanning import cleanup as cleanup_mod

        deleted = []

        def fake_delete(track_ids, context=None):
            deleted.extend(track_ids)
            return len(track_ids)

        monkeypatch.setattr(cleanup_mod, "delete_tracks_by_id", fake_delete)
        monkeypatch.setattr(cleanup_mod, "log_unified", lambda *a, **k: None)

        cleanup_mod.cleanup_stale_artist_tracks_if_needed(
            artist_name="Muse",
            existing_track_ids={"a", "b", "c", "d"},
            navidrome_track_ids={"a", "b"},
        )

        assert sorted(deleted) == ["c", "d"]
