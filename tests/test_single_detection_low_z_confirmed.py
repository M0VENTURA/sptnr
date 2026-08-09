"""Regression tests: confirmed singles must not be demoted by a low z-score.

Reproduces the reported miss: a track confirmed as a single by Discogs (0.8 —
below 100%, so a MEDIUM source) AND MusicBrainz AND Last.fm (medium sources)
came back as ``medium``-confidence / 4★ purely because its popularity sat below
the artist median (artist_z < -1.0, the ``z_low`` soft-gate). MusicBrainz and
Last.fm independently corroborate the Discogs match, so the verdict must stay
``high`` (5★-eligible) regardless of z-score.

The gate still demotes a LONE sub-100% Discogs match with zero corroboration at
a low z-score — popularity must never promote a metadata-thin verdict to 5★.
"""

from __future__ import annotations


def _patch_sources(monkeypatch, discogs=True, musicbrainz=True, lastfm=True, release_year=2006):
    import services.popularity.popularity_stats_service as pss
    from services.enrichment import single_detection_service as sds

    def fake_discogs(*args, **kwargs):
        return {
            "source": "discogs",
            "matched": discogs,
            "confidence": 0.8 if discogs else 0.0,
            "metadata": {"release_year": release_year} if discogs and release_year else {},
        }

    def fake_mb(*args, **kwargs):
        return {
            "source": "musicbrainz",
            "matched": musicbrainz,
            "confidence": 0.9 if musicbrainz else 0.0,
            "metadata": {},
        }

    class FakeLastFm:
        def check_track_as_single(self, artist, title):
            return lastfm

        def get_album_track_count(self, artist, album):
            return 0

    # Artist median of 80 with a 68-popularity track forces artist_z ≈ -1.2,
    # i.e. ``z_low`` is True while album stats stay empty (album_z = 0).
    def fake_artist_stats(conn, artist):
        return (80.0, 0.0, [80, 80, 80, 80, 80, 80, 80, 80, 80, 80])

    def fake_album_stats(conn, artist, album):
        return (0.0, 0.0, [])

    monkeypatch.setattr(sds, "_detect_discogs", fake_discogs)
    monkeypatch.setattr(sds, "_detect_musicbrainz", fake_mb)
    monkeypatch.setattr(pss, "calculate_artist_stats", fake_artist_stats)
    monkeypatch.setattr(pss, "calculate_album_stats", fake_album_stats)
    return FakeLastFm()


def _run(monkeypatch, discogs=True, musicbrainz=True, lastfm=True, release_year=2006):
    from services.enrichment.single_detection_service import detect_single_for_track

    lastfm_client = _patch_sources(
        monkeypatch, discogs=discogs, musicbrainz=musicbrainz, lastfm=lastfm, release_year=release_year
    )
    return detect_single_for_track(
        title="Some Confirmed Single",
        artist="Big Artist",
        album="Some Album",
        album_track_count=14,
        popularity=68.0,
        album_type="album",
        use_advanced_detection=True,
        persist_result=False,
        discogs_token="test-token",
        lastfm_client=lastfm_client,
    )


class TestConfirmedSingleBelowMedian:
    def test_discogs_plus_mb_lastfm_stays_high(self, monkeypatch):
        # Discogs (0.8, MEDIUM) + MusicBrainz + Last.fm all confirm, but the
        # track sits below the artist median (artist_z < -1.0). The z_low gate
        # must NOT demote a corroborated 'high' verdict to 'medium' — the
        # confirmed single keeps its 5★ eligibility.
        result = _run(monkeypatch, discogs=True, musicbrainz=True, lastfm=True)
        assert result["is_single"] is True
        assert result["confidence"] == "high"
        assert result["decision"]["z_low"] is True
        assert result["decision"]["high_sources"] == 0
        assert result["decision"]["medium_sources"] >= 2

    def test_discogs_plus_mb_stays_high(self, monkeypatch):
        # A sub-100% Discogs match plus ANY independent medium source is
        # enough — no Last.fm needed.
        result = _run(monkeypatch, discogs=True, musicbrainz=True, lastfm=False)
        assert result["is_single"] is True
        assert result["confidence"] == "high"
        assert result["decision"]["z_low"] is True
        assert result["decision"]["high_sources"] == 0
        assert result["decision"]["medium_sources"] >= 2

    def test_lone_high_source_still_demoted(self, monkeypatch):
        # The gate's purpose survives: a sub-100% Discogs match with NO other
        # source at a low z-score still caps at 'medium' — popularity must not
        # promote a metadata-thin verdict to 5★. (No release-year metadata so
        # the release-date signal can't add a corroborating medium source.)
        result = _run(monkeypatch, discogs=True, musicbrainz=False, lastfm=False, release_year=None)
        assert result["is_single"] is True
        assert result["confidence"] == "medium"
        assert result["decision"]["z_low"] is True
        assert result["decision"]["high_sources"] == 0
        assert result["decision"]["medium_sources"] == 1
