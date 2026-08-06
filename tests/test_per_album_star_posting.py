"""Regression tests for per-album star rating posting.

The staged runner used to defer ALL star-rating assignment/persistence/logging
to ``finalise_scan`` at the end of the whole run, so a full artist scan only
posted the "Star Ratings - Album ..." summary once everything had finished.
The legacy scanner posted each album's ratings right after the album
completed.  These tests cover:

- ``post_album_star_ratings`` assigns, persists, logs and syncs one album's
  ratings and returns counts.
- ``finalise_scan`` honors ``options["_per_album_posted"]`` — when the runner
  already posted per-album ratings during the scan loop, finalise skips the
  (now redundant) per-album work but still writes artist_stats and the
  essential playlist.
"""

from __future__ import annotations

import pytest


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _album_results():
    return [
        {
            "track_id": "t1",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Apocalypse Please",
            "popularity_score": 80.0,
            "final_score": 80.0,
            "lastfm_listeners": 500,
            "listenbrainz_listens": 4000,
            "lastfm_score": 6.0,
            "listenbrainz_score": 7.0,
            "is_single": False,
            "single_confidence": "low",
        },
        {
            "track_id": "t2",
            "artist": "Muse",
            "album": "Absolution",
            "title": "Time Is Running Out",
            "popularity_score": 90.0,
            "final_score": 90.0,
            "lastfm_listeners": 1000,
            "listenbrainz_listens": 8000,
            "lastfm_score": 8.0,
            "listenbrainz_score": 9.0,
            "is_single": True,
            "single_confidence": "high",
        },
    ]


class TestPostAlbumStarRatings:
    def test_assigns_persists_logs_and_syncs(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        conn = FakeConn()
        # Deterministic star value per track so assertions are stable.
        monkeypatch.setattr(
            fs, "_assign_stars", lambda track, *args: 4 if track["title"] == "Apocalypse Please" else 5
        )
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda track_id, stars: True)
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
            conn=conn,
            cursor=conn.cur,
        )

        assert outcome["star_ratings"] == 2
        assert outcome["navidrome_synced"] == 2
        assert [t["stars"] for t in results] == [4, 5]

        # Each rated track persisted its star rating.
        star_updates = [e for e in conn.cur.executed if "UPDATE tracks SET stars" in e[0]]
        assert len(star_updates) == 2
        assert (5, "t2") in {e[1] for e in star_updates}

        # Per-album summary was emitted.
        assert any("Star Ratings - Album 'Absolution' by Muse" in m for m in logged)
        assert any("Single Detection Scan - ===== Absolution - Detected Singles =====" in m for m in logged)

    def test_returns_zero_for_empty_album(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        conn = FakeConn()
        outcome = fs.post_album_star_ratings(
            album_results=[],
            artist="Muse",
            artist_scores=[],
            options={},
            conn=conn,
            cursor=conn.cur,
        )
        assert outcome == {"star_ratings": 0, "navidrome_synced": 0}
        assert conn.cur.executed == []


class TestFinaliseScanPerAlbumFlag:
    def test_per_album_posted_skips_duplicate_star_work(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        conn = FakeConn()
        monkeypatch.setattr(fs, "get_db_connection", lambda: conn)
        posted = []
        monkeypatch.setattr(
            fs,
            "post_album_star_ratings",
            lambda **kw: posted.append(kw),
        )
        monkeypatch.setattr(fs, "_create_nsp_playlist", lambda artist, tracks: None)
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        fs.finalise_scan(
            results=results,
            options={
                "_per_album_posted": True,
                "_per_album_posted_keys": {("Muse", "Absolution")},
            },
        )

        # Runner already posted per-album ratings → finalise must not re-assign.
        assert posted == []
        # artist_stats write still happened (album/artist context data).
        stats_writes = [e for e in conn.cur.executed if "INSERT INTO artist_stats" in e[0]]
        assert len(stats_writes) == 1

    def test_without_flag_delegates_per_album(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        conn = FakeConn()
        monkeypatch.setattr(fs, "get_db_connection", lambda: conn)
        posted = []
        monkeypatch.setattr(
            fs,
            "post_album_star_ratings",
            lambda **kw: posted.append(kw) or {"star_ratings": 2, "navidrome_synced": 0},
        )
        monkeypatch.setattr(fs, "_create_nsp_playlist", lambda artist, tracks: None)
        monkeypatch.setattr(fs, "log_unified", lambda msg: None)

        fs.finalise_scan(
            results=_album_results(),
            options={"sync_navidrome": True},
        )

        assert len(posted) == 1
        assert posted[0]["album_results"][0]["track_id"] == "t1"


class TestListenerZRobustness:
    """_listener_z must never divide by zero.

    An album whose positive listener/listen counts are all the same value
    (e.g. a tracklist fallback that resolved every track to the same count)
    has zero variance — the old code computed ``stdev == 0`` and crashed the
    whole album's star-rating pass with ``ZeroDivisionError``, leaving every
    track unrated.
    """

    def test_zero_variance_distribution_returns_zero(self):
        from services.popularity.stages import finalise_stage as fs

        # 3+ identical positive counts → sigma == 0 → must not raise.
        assert fs._listener_z(300, [300, 300, 300]) == 0.0
        assert fs._listener_z(500, [500, 500, 500, 500]) == 0.0

    def test_mixed_counts_still_score(self):
        from services.popularity.stages import finalise_stage as fs

        z = fs._listener_z(1000, [100, 200, 300, 1000])
        assert z > 1.0

    def test_assign_stars_survives_identical_counts(self):
        from services.popularity.stages import finalise_stage as fs

        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": 40.0, "final_score": 40.0,
            "lastfm_listeners": 300, "listenbrainz_listens": 300,
            "lb_percentile": 0.5, "lastfm_score": 4.0, "listenbrainz_score": 4.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False,
        }
        stars = fs._assign_stars(track, [40.0, 40.0, 40.0], [40.0, 40.0, 40.0],
                                 [300, 300, 300], [300, 300, 300])
        assert 1 <= stars <= 5


class TestPostAlbumStarRatingsResilience:
    def test_one_track_failure_does_not_abort_album(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        conn = FakeConn()

        def flaky_assign(track, *args):
            if track["title"] == "Time Is Running Out":
                raise RuntimeError("boom")
            return 4

        monkeypatch.setattr(fs, "_assign_stars", flaky_assign)
        monkeypatch.setattr(fs, "_sync_rating_to_navidrome", lambda *a, **k: True)
        logged = []
        monkeypatch.setattr(fs, "log_unified", lambda msg: logged.append(msg))

        results = _album_results()
        outcome = fs.post_album_star_ratings(
            album_results=results,
            artist="Muse",
            artist_scores=[80.0, 90.0],
            options={"sync_navidrome": True},
            conn=conn,
            cursor=conn.cur,
        )

        # The failing track is skipped; the healthy one still gets rated.
        assert outcome["star_ratings"] == 1
        assert results[0]["stars"] == 4
        assert results[1].get("stars") is None
        star_updates = [e for e in conn.cur.executed if "UPDATE tracks SET stars" in e[0]]
        assert len(star_updates) == 1
        assert any("Star Ratings - Album 'Absolution' by Muse" in m for m in logged)
