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
