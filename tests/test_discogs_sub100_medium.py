"""Regression tests: a sub-100% Discogs match is a MEDIUM-confidence source.

A Discogs single match carries 0.8 confidence (promos 0.5) — never 1.0 — so
it must be counted as a MEDIUM source, not high.  A lone Discogs match must
therefore NOT reach 'high' on its own (false-positive 5★ fix).  It reaches
'high' only when corroborated by an independent medium-confidence source or
the popularity standout (``z_standout``).

Reproduces the "Extremist Makeover" scan output:
- Use It / Birthday / Like I Do: Discogs + MusicBrainz (+ Last.fm) all confirm
  -> stay 'high'.
- Runaway: only Discogs confirms -> drops from 'high' to 'medium'.
"""

from __future__ import annotations


def _run(monkeypatch, *, discogs=True, musicbrainz=False, lastfm=False,
         release_year=None, popularity=50.0):
    import services.popularity.popularity_stats_service as pss
    from services.enrichment import single_detection_service as sds
    from services.enrichment.single_detection_service import detect_single_for_track

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

    monkeypatch.setattr(sds, "_detect_discogs", fake_discogs)
    monkeypatch.setattr(sds, "_detect_musicbrainz", fake_mb)

    # Artist catalogue of 20 tracks around 50 → the track at 50 sits at the
    # median (album/artist z ≈ 0), so no standalone z_standout fires unless
    # the test explicitly forces the source list to carry it.
    def fake_artist_stats(conn, artist):
        return (50.0, 10.0, [50] * 20)

    def fake_album_stats(conn, artist, album):
        return (50.0, 10.0, [50, 50, 50, 50, 50])

    monkeypatch.setattr(pss, "calculate_artist_stats", fake_artist_stats)
    monkeypatch.setattr(pss, "calculate_album_stats", fake_album_stats)

    lastfm_client = FakeLastFm() if lastfm else None
    return detect_single_for_track(
        title="Some Track",
        artist="Some Artist",
        album="Some Album",
        album_track_count=10,
        popularity=popularity,
        album_type="album",
        use_advanced_detection=True,
        persist_result=False,
        discogs_token="test-token",
        lastfm_client=lastfm_client,
    )


def _run_standout(monkeypatch, *, discogs=True):
    """Force a ``popularity_z_standout`` source alongside the Discogs match."""
    import services.popularity.popularity_stats_service as pss
    from services.enrichment import single_detection_service as sds

    def fake_discogs(*args, **kwargs):
        return {"source": "discogs", "matched": discogs,
                "confidence": 0.8 if discogs else 0.0,
                "metadata": {"release_year": 2004} if discogs else {}}

    def fake_mb(*args, **kwargs):
        return {"source": "musicbrainz", "matched": False,
                "confidence": 0.0, "metadata": {}}

    monkeypatch.setattr(sds, "_detect_discogs", fake_discogs)
    monkeypatch.setattr(sds, "_detect_musicbrainz", fake_mb)

    # A tight artist distribution so the track at 70 is a strong outlier
    # (album/artist z ≈ 2.0 → above the dynamic threshold ≈ 1.8 → z_standout).
    def fake_artist_stats(conn, artist):
        return (50.0, 10.0, [50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50])

    def fake_album_stats(conn, artist, album):
        return (50.0, 10.0, [50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50])

    monkeypatch.setattr(pss, "calculate_artist_stats", fake_artist_stats)
    monkeypatch.setattr(pss, "calculate_album_stats", fake_album_stats)

    from services.enrichment.single_detection_service import detect_single_for_track
    return detect_single_for_track(
        title="Some Track",
        artist="Some Artist",
        album="Some Album",
        album_track_count=10,
        popularity=70.0,
        album_type="album",
        use_advanced_detection=True,
        persist_result=False,
        discogs_token="test-token",
    )


class TestDiscogsSub100IsMedium:
    def test_lone_discogs_match_is_medium(self, monkeypatch):
        # Runaway case: only Discogs confirms (0.8).  Must NOT reach 'high' —
        # this is the false-positive fix.  (No release-year metadata, mirroring
        # Runaway's bare ``{'is_promo': False}`` metadata.)
        result = _run(monkeypatch, discogs=True, musicbrainz=False, lastfm=False, release_year=None)
        assert result["is_single"] is True
        assert result["confidence"] == "medium"
        assert result["decision"]["high_sources"] == 0
        assert result["decision"]["medium_sources"] == 1

    def test_discogs_plus_second_medium_is_high(self, monkeypatch):
        # Use It / Like I Do case: Discogs + MusicBrainz (+ Last.fm) all
        # confirm -> a second independent medium source corroborates, so the
        # verdict reaches 'high'.
        result = _run(monkeypatch, discogs=True, musicbrainz=True, lastfm=False)
        assert result["is_single"] is True
        assert result["confidence"] == "high"

    def test_discogs_plus_lastfm_is_high(self, monkeypatch):
        result = _run(monkeypatch, discogs=True, musicbrainz=False, lastfm=True)
        assert result["is_single"] is True
        assert result["confidence"] == "high"

    def test_discogs_plus_popularity_standout_is_high(self, monkeypatch):
        # The popularity metric (z_standout) gives a lone Discogs match 'high'.
        result = _run_standout(monkeypatch, discogs=True)
        assert result["is_single"] is True
        assert result["confidence"] == "high"
        assert "popularity_z_standout" in [s.get("source") for s in result["sources"]]

    def test_full_confidence_discogs_counts_as_high_source(self, monkeypatch):
        # A hypothetical 1.0-consistent Discogs match stays a HIGH source.
        from services.enrichment.single_detection_service import detect_single_for_track
        from services.enrichment import single_detection_service as sds

        def fake_discogs(*args, **kwargs):
            return {"source": "discogs", "matched": True, "confidence": 1.0, "metadata": {}}

        monkeypatch.setattr(sds, "_detect_discogs", fake_discogs)

        def fake_artist_stats(conn, artist):
            return (50.0, 10.0, [50, 50, 50, 50, 50, 50, 50, 50, 50, 50])

        def fake_album_stats(conn, artist, album):
            return (50.0, 10.0, [50, 50, 50, 50, 50])

        import services.popularity.popularity_stats_service as pss
        monkeypatch.setattr(pss, "calculate_artist_stats", fake_artist_stats)
        monkeypatch.setattr(pss, "calculate_album_stats", fake_album_stats)

        result = detect_single_for_track(
            title="Some Track", artist="Some Artist", album="Some Album",
            album_track_count=10, popularity=50.0, album_type="album",
            use_advanced_detection=True, persist_result=False,
            discogs_token="test-token",
        )
        assert result["decision"]["high_sources"] == 1
        assert result["confidence"] == "high"
