"""Tests for Work-level ListenBrainz aggregation.

Covers ``get_work_level_listenbrainz_popularity`` — the fix for splintered
single scrobbles: a song released as a single has separate MusicBrainz
recordings (album cut, 7" single edit, Greatest Hits master, radio promo)
that all perform the SAME Work, so ListenBrainz counts are summed across
the same-artist recordings of that Work instead of reading only the
release-pinned recording.
"""

from __future__ import annotations

import pytest


class _FakeMBClient:
    """Stands in for MusicBrainzHttpClient with a seeded recording + Work."""

    def __init__(self, seed_recording=None, work_recordings=None):
        self._seed = seed_recording or {}
        self._work = work_recordings or []
        self._isrc_recordings = []

    def get_recording(self, recording_mbid, inc="", timeout=10.0):
        return dict(self._seed)

    def browse_work_recordings(self, work_mbid, inc="artist-credits", limit=100):
        return list(self._work)

    def lookup_by_isrc(self, isrc, inc=""):
        return list(self._isrc_recordings)


_COUNTS = {
    "rec-single": {"total_listen_count": 550200, "total_user_count": 120000},
    "rec-album": {"total_listen_count": 1015, "total_user_count": 300},
    "rec-hits": {"total_listen_count": 250500, "total_user_count": 60000},
    "rec-cover": {"total_listen_count": 999999, "total_user_count": 500000},
    "rec-live": {"total_listen_count": 50000, "total_user_count": 10000},
}


def _fake_lb_batch(mbids):
    return {
        m: _COUNTS.get(m, {"total_listen_count": 0, "total_user_count": 0})
        for m in mbids
    }


def _seed_recording(rec_id="rec-album", work_id="work-judith", artist_mbid="art-1"):
    return {
        "id": rec_id,
        "title": "Judith",
        "relations": [
            {
                "type": "performance",
                "direction": "forward",
                "work": {"id": work_id, "title": "Judith"},
            }
        ],
        "artist-credit": [
            {"artist": {"id": artist_mbid, "name": "A Perfect Circle"}, "name": "A Perfect Circle"}
        ],
    }


def _work_recordings(artist_mbid="art-1"):
    """Recordings linked to the Work: 3 by the artist, 1 cover, 1 live."""
    def _rec(rec_id, title, aid, aname):
        return {
            "id": rec_id,
            "title": title,
            "artist-credit": [{"artist": {"id": aid, "name": aname}, "name": aname}],
        }

    return [
        _rec("rec-single", "Judith", artist_mbid, "A Perfect Circle"),
        _rec("rec-album", "Judith", artist_mbid, "A Perfect Circle"),
        _rec("rec-hits", "Judith", artist_mbid, "A Perfect Circle"),
        # Cover by another artist — the Work links it too, must be excluded.
        _rec("rec-cover", "Judith", "art-2", "Failure"),
        # Live performance — a different performance, must be excluded.
        _rec("rec-live", "Judith (Live)", artist_mbid, "A Perfect Circle"),
    ]


def _run_work_aggregation(monkeypatch, **kwargs):
    from services.popularity import popularity_sources as ps

    monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _fake_lb_batch)
    defaults = {
        "title": "Judith",
        "artist": "A Perfect Circle",
        "artist_mbid": "art-1",
        "primary_mbid": "rec-album",
        "isrc": "",
        "mb_client": _FakeMBClient(
            seed_recording=_seed_recording(),
            work_recordings=_work_recordings(),
        ),
    }
    defaults.update(kwargs)
    return ps.get_work_level_listenbrainz_popularity(**defaults)


def test_work_aggregation_sums_same_artist_recordings(monkeypatch):
    """Single edit + album cut + Greatest Hits master all share the Work and
    are summed; the cover by another artist and the live take are excluded."""
    result = _run_work_aggregation(monkeypatch)

    assert result["source"] == "work"
    assert result["work_mbid"] == "work-judith"
    # 550200 (single edit) + 1015 (album cut) + 250500 (Greatest Hits) = 801715
    assert result["total_listen_count"] == 550200 + 1015 + 250500
    assert result["total_user_count"] == 120000 + 300 + 60000
    assert "rec-cover" not in result["mbids"]
    assert "rec-live" not in result["mbids"]
    assert result["mbids"] == ["rec-album", "rec-hits", "rec-single"]


def test_work_aggregation_falls_back_to_artist_name(monkeypatch):
    """Without an artist MBID, the cover is still excluded via normalized
    artist-name equality."""
    result = _run_work_aggregation(monkeypatch, artist_mbid="")
    assert "rec-cover" not in result["mbids"]
    assert result["total_listen_count"] == 550200 + 1015 + 250500


def test_no_work_resolvable_returns_zeros(monkeypatch):
    """A recording without work-rels must fall back to zero counts, not a
    crash."""
    from services.popularity import popularity_sources as ps

    monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _fake_lb_batch)
    client = _FakeMBClient(
        seed_recording={
            "id": "rec-album",
            "title": "Judith",
            "relations": [],
            "artist-credit": [],
        },
        work_recordings=[],
    )
    result = ps.get_work_level_listenbrainz_popularity(
        title="Judith", artist="A Perfect Circle",
        primary_mbid="rec-album", mb_client=client,
    )
    assert result["source"] == "work"
    assert result["work_mbid"] == ""
    assert result["total_listen_count"] == 0
    assert result["mbids"] == []


def test_isrc_seeds_work_lookup_when_no_recording_mbid(monkeypatch):
    """When the track has no recording MBID, the ISRC resolves the recording
    first and the Work is found from it."""
    from services.popularity import popularity_sources as ps

    monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _fake_lb_batch)
    client = _FakeMBClient(
        seed_recording=_seed_recording(rec_id="rec-isrc-rec"),
        work_recordings=_work_recordings(),
    )
    monkeypatch.setattr(
        ps,
        "resolve_isrc_recording",
        lambda isrc, title="", artist="", mb_client=None: {
            "recording_mbid": "rec-isrc-rec",
            "title": "Judith",
            "artist": "A Perfect Circle",
        },
    )
    result = ps.get_work_level_listenbrainz_popularity(
        title="Judith", artist="A Perfect Circle",
        artist_mbid="art-1", isrc="USVI20301027", mb_client=client,
    )
    assert result["work_mbid"] == "work-judith"
    assert result["total_listen_count"] == 550200 + 1015 + 250500


def test_no_same_artist_recordings_returns_zeros(monkeypatch):
    """A Work whose recordings are all by other artists yields zero counts."""
    from services.popularity import popularity_sources as ps

    monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _fake_lb_batch)
    client = _FakeMBClient(
        seed_recording=_seed_recording(),
        work_recordings=_work_recordings(artist_mbid="art-9"),
    )
    # The seeded album recording is by art-1, so seed_mbids keeps it — use a
    # primary_mbid that is NOT in the work browse so only foreign artists remain.
    result = ps.get_work_level_listenbrainz_popularity(
        title="Judith", artist="A Perfect Circle",
        artist_mbid="art-1", primary_mbid="rec-other", mb_client=client,
    )
    assert result["total_listen_count"] == 0
    # The seed itself is still summed (its own count), which is the correct
    # floor when no other same-artist recordings exist.
    assert result["mbids"] == ["rec-other"]


class TestWorkMbidHintSkipsPerTrackFetch:
    def test_hint_skips_get_recording(self, monkeypatch):
        """When ``work_mbid_hint`` is supplied, the per-track
        ``get_recording(work-rels)`` MusicBrainz call must be SKIPPED —
        the caller already resolved the work MBID from the release data.
        This is the 1 req/s bottleneck saving."""
        from services.popularity import popularity_sources as ps

        calls = {"get_recording": 0, "browse": 0}

        class _CountingMBClient:
            def get_recording(self, recording_mbid, inc="", timeout=10.0):
                calls["get_recording"] += 1
                return _seed_recording()

            def browse_work_recordings(self, work_mbid, inc="artist-credits", limit=100):
                calls["browse"] += 1
                return _work_recordings()

        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _fake_lb_batch)

        result = ps.get_work_level_listenbrainz_popularity(
            title="Judith",
            artist="A Perfect Circle",
            artist_mbid="art-1",
            primary_mbid="rec-album",
            mb_client=_CountingMBClient(),
            work_mbid_hint="work-judith",  # already known from release work-rels
        )

        assert result["work_mbid"] == "work-judith"
        # The work browse still runs (needed for the recording list)…
        assert calls["browse"] == 1
        # …but the per-track get_recording(work-rels) call is skipped.
        assert calls["get_recording"] == 0
        # Same-artist recordings still summed (cover/live excluded).
        assert result["total_listen_count"] == 550200 + 1015 + 250500

    def test_no_hint_still_fetches_recording(self, monkeypatch):
        """Without a hint, the legacy path fetches the recording to resolve
        the work MBID (backward compatible)."""
        from services.popularity import popularity_sources as ps

        calls = {"get_recording": 0, "browse": 0}

        class _CountingMBClient:
            def get_recording(self, recording_mbid, inc="", timeout=10.0):
                calls["get_recording"] += 1
                return _seed_recording()

            def browse_work_recordings(self, work_mbid, inc="artist-credits", limit=100):
                calls["browse"] += 1
                return _work_recordings()

        monkeypatch.setattr(ps, "lb_get_recording_popularity_batch", _fake_lb_batch)

        ps.get_work_level_listenbrainz_popularity(
            title="Judith",
            artist="A Perfect Circle",
            artist_mbid="art-1",
            primary_mbid="rec-album",
            mb_client=_CountingMBClient(),
        )

        assert calls["get_recording"] == 1
