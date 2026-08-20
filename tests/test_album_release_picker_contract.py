"""Tests for the album release-picker contract fix.

Symptom: on the album page, using the MusicBrainz lookup → select an album
→ "The automatic match was not confident enough" modal opens but NO releases
load.

Root cause: ``get_release_group_releases`` returned RAW MusicBrainz release
dicts (``media`` arrays), but the release-picker renderer expects the
normalised shape (``formats`` list, ``disc_count``, ``track_count``,
``disambiguation``, ``cover_art_url``).  ``r.formats.join(' + ')`` threw on
the raw shape, the render died silently and the results div stayed empty.

Fixes pinned here:
1. ``get_release_group_releases`` normalises every release to the same
   contract ``get_musicbrainz_best_release`` uses.
2. ``get_musicbrainz_best_release`` browses with ``inc=media+labels`` (the
   release browse endpoint can reject some inc combinations).
"""

from __future__ import annotations

import pytest


class TestReleaseGroupReleasesNormalization:
    def test_returns_normalized_release_shape(self, monkeypatch):
        """Raw MB releases (media arrays) become the picker contract."""
        from services.enrichment import musicbrainz_service as mbs

        class _FakeClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                return {
                    "releases": [
                        {
                            "id": "rel-1",
                            "title": "The Album",
                            "date": "2024-05-01",
                            "country": "US",
                            "status": "Official",
                            "disambiguation": "",
                            "media": [
                                {"format": "CD", "track-count": 10},
                                {"format": "CD", "track-count": 2},
                            ],
                        },
                    ],
                }

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        result = mbs.get_release_group_releases("rg-abc")
        assert result["success"] is True
        rel = result["releases"][0]
        assert rel["id"] == "rel-1"
        assert rel["formats"] == ["CD"]
        assert rel["disc_count"] == 2
        assert rel["track_count"] == 12
        assert rel["cover_art_url"].startswith("https://coverartarchive.org/release/rel-1/")

    def test_no_media_yields_empty_formats(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        class _FakeClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                return {"releases": [{"id": "rel-1", "title": "X"}]}

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        result = mbs.get_release_group_releases("rg-abc")
        rel = result["releases"][0]
        assert rel["formats"] == []
        assert rel["track_count"] == 0
        assert rel["disc_count"] == 0

    def test_error_returns_success_false(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        class _BoomClient:
            def get_release_group(self, rg_mbid, inc="", timeout=10.0):
                raise RuntimeError("boom")

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _BoomClient())
        result = mbs.get_release_group_releases("rg-abc")
        assert result["success"] is False
        assert "error" in result


class TestBestReleaseBrowseInc:
    def test_browse_uses_media_plus_labels(self, monkeypatch):
        """best-release must NOT request ``recordings`` on the browse call —
        the release browse endpoint can reject inc combinations."""
        from services.enrichment import musicbrainz_service as mbs

        captured = {}

        class _FakeClient:
            def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
                captured["inc"] = inc
                return []

        # _get_local_track_count hits the DB — stub it to 0.
        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeClient())
        monkeypatch.setattr(mbs, "_get_local_track_count", lambda *a, **k: 0)
        result = mbs.get_musicbrainz_best_release("Artist", "Album", "rg-abc")
        assert captured["inc"] == "media+labels"
        assert "releases" in result
