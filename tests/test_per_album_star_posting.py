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


class TestAlbumZBandStars:
    """Spec rule 4: 1-4★ come purely from the album's popularity z-score bands.

    After 5★ singles/standouts are assigned, the rest of the album is ranked by
    its intra-album z-score: Z >= +0.5 → 4★, -0.5 <= Z < +0.5 → 3★,
    -1.2 <= Z < -0.5 → 2★, Z < -1.2 → 1★.  Artist context never affects the
    base rating, and popularity alone never reaches 5★.
    """

    def _track(self, score, **overrides):
        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": score, "final_score": score,
            "lastfm_listeners": 100, "listenbrainz_listens": 100,
            "lb_percentile": 0.5, "lastfm_score": 4.0, "listenbrainz_score": 4.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }
        track.update(overrides)
        return track

    def _album(self):
        # 12 tracks spread 1-100 → mean 50.5, sample stdev ~32.45, so:
        # 4★ for 100/91/82/73 (z >= +0.5), 3★ for 64/46, 2★ for 28/19,
        # 1★ for 10/1 (z < -1.2).
        return list(range(1, 101, 9))[:12]  # [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]

    def _stars(self, score, album, **overrides):
        from services.popularity.stages import finalise_stage as fs
        return fs._assign_stars(self._track(score, **overrides), album, album)

    def test_top_band_gets_four(self):
        # Z >= +0.5 → 4★.
        album = self._album()
        assert self._stars(100.0, album) == 4
        assert self._stars(73.0, album) == 4

    def test_middle_band_gets_three(self):
        # -0.5 <= Z < +0.5 → 3★.
        assert self._stars(64.0, self._album()) == 3
        assert self._stars(46.0, self._album()) == 3

    def test_lower_band_gets_two(self):
        # -1.2 <= Z < -0.5 → 2★.
        assert self._stars(28.0, self._album()) == 2
        assert self._stars(19.0, self._album()) == 2

    def test_bottom_outliers_gets_one(self):
        # Z < -1.2 → 1★.
        assert self._stars(1.0, self._album()) == 1
        assert self._stars(10.0, self._album()) == 1

    def test_zero_score_is_one_star(self):
        assert self._stars(0.0, self._album()) == 1

    def test_high_confidence_single_gets_five_regardless_of_band(self):
        # Spec rule 5: a high-confidence single is 5★ even at the bottom of a
        # strong album — singles are exempt from the album-z-band base (as
        # long as the ORGANIC popularity floor is met: score >= 45 or >= 1000
        # Last.fm listeners).
        assert self._stars(
            10.0, self._album(), is_single=True, single_confidence="high",
            lastfm_listeners=2000,
        ) == 5

    def test_high_confidence_single_below_organic_floor_stays_on_band(self):
        # A metadata-tagged single with almost no organic audience (e.g.
        # Discogs-confirmed with 299 listeners and a sub-45 score) must not
        # leapfrog genuinely popular album tracks — it keeps the album-z band
        # rating instead of the forced single promotion.
        from services.popularity.stages import finalise_stage as fs

        assert self._stars(
            10.0, self._album(), is_single=True, single_confidence="high",
            lastfm_listeners=299,
        ) == 1
        # The same gate caps the 4★ Single Floor: a non-organic high single
        # that misses the era bar never exceeds 3★ (era-model path).
        track = self._track(
            46.0, is_single=True, single_confidence="high", lastfm_listeners=299
        )
        album_model = {"has_benchmark": True, "era": "peak", "catalog_cutoff": 80.0,
                       "max_5star_slots": 4}
        assert fs._assign_stars(track, self._album(), self._album(), album_model=album_model) <= 3

    def test_medium_single_not_marked_follows_album_band(self):
        # A medium-confidence single that is NOT popularity-marked stays on the
        # album-z-band base (no 5★) unless it is a genuine standout.
        album = self._album()
        assert self._stars(46.0, album, is_single=True, single_confidence="medium") == 3

    def test_popularity_marked_standout_gets_five(self):
        # A non-single triple-standout (album z + artist z + top-10% marking)
        # reaches 5★ per spec rule 5.
        album = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]
        assert self._stars(100.0, album, popularity_marked=True) == 5

    def test_popularity_marked_alone_gets_five_without_single_source(self):
        # Spec rule 2: a track in the artist's top 10% is "popular" — the
        # marking alone grants 5★ even though it is NOT a high-confidence
        # single and does NOT clear the album/artist standout thresholds.
        album = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]
        assert self._stars(46.0, album, popularity_marked=True, is_single=False,
                           single_confidence="low") == 5

    def test_z_standout_source_counts_as_popularity_proof(self):
        # The popularity_z_standout detection signal is an alternative proof for
        # the 5★ standout condition.
        from services.popularity.stages import finalise_stage as fs
        album = [1, 10, 19, 28, 37, 46, 55, 64, 73, 82, 91, 100]
        track = self._track(
            100.0,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, album, album) == 5


class TestZStandoutSourceReVerification:
    """A ``popularity_z_standout`` source is re-verified against the album's raw
    listener distribution before it can grant 5★.

    On a standout album (one album far more popular than the rest of an
    artist's catalogue) every track's artist_z is inflated, so the old code
    marked whole albums as ``popularity_z_standout`` and promoted them to 5★.
    The scan re-verifies the flag via the track's composite LF/LB listener z
    (``listener_5star_z_threshold``) so only genuine intra-album standouts
    reach 5★ from popularity.
    """

    def _track(self, score, lastfm, lb, **overrides):
        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": score, "final_score": score,
            "lastfm_listeners": lastfm, "listenbrainz_listens": lb,
            "lb_percentile": 0.5, "lastfm_score": 4.0, "listenbrainz_score": 4.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }
        track.update(overrides)
        return track

    def _album(self):
        return [40.0, 50.0, 60.0, 70.0, 80.0, 90.0]

    def _listeners(self):
        return [10000, 9000, 8000, 7000, 6000, 5000], [20000, 18000, 16000, 14000, 12000, 10000]

    def test_mid_pack_listeners_do_not_honour_standout_flag(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listeners()
        # Album z AND artist z clear the 5★ standout thresholds, and the track
        # carries a popularity_z_standout source — but its RAW listener counts
        # sit mid-pack in the album, so the re-verification must NOT honour the
        # flag. Popularity alone never reaches 5★.
        track = self._track(
            90.0, 6000, 12000,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, self._album(), self._album(), lf, lb) == 4

    def test_album_top_listeners_honour_standout_flag(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listeners()
        # The clear intra-album listener standout IS honoured → 5★.
        track = self._track(
            90.0, 10000, 20000,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, self._album(), self._album(), lf, lb) == 5

    def test_no_album_listener_distribution_keeps_flag(self):
        from services.popularity.stages import finalise_stage as fs

        # No listener distribution → verification is skipped, flag honoured.
        track = self._track(
            90.0, 6000, 12000,
            single_sources='[{"source": "popularity_z_standout", "matched": true, "confidence": 0.5}]',
        )
        assert fs._assign_stars(track, self._album(), self._album()) == 5

    def test_popularity_marked_standout_not_listener_gated(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listeners()
        # The artist top-10% marking is a DIFFERENT proof path (not the
        # z_standout source) — the listener re-verification does not apply.
        track = self._track(90.0, 6000, 12000, popularity_marked=True)
        assert fs._assign_stars(track, self._album(), self._album(), lf, lb) == 5


class TestPostAlbumStarRatings:
    def test_assigns_persists_logs_and_syncs(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        conn = FakeConn()
        # Deterministic star value per track so assertions are stable.
        monkeypatch.setattr(
            fs, "_assign_stars",
            lambda track, *args, **kwargs: 4 if track["title"] == "Apocalypse Please" else 5
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

        # Per-album tabular summary was emitted (new format).
        assert any("📊 SCAN RESULTS: Muse — Absolution" in m for m in logged)
        assert any("Singles Detection - Detected 1 single(s) in 'Absolution'" in m for m in logged)
        assert any("⭐ Distribution" in m for m in logged)

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


class TestAssignStarsPopularityOnly:
    """A popularity-only pass rates on popularity alone.

    Single detection did not run, so single status must not drive the rating.
    The album-relative spec reserves 5★ for high-confidence singles and
    genuine triple-standouts (album z + artist z + a popularity standout) —
    popularity alone never grants 5★.
    """

    def _artist_scores(self):
        return [50, 52, 54, 56, 58, 60, 62, 64, 66, 90]

    def _album_scores(self):
        return [50, 55, 60, 65, 90]

    def _base_track(self, **overrides):
        track = {
            "track_id": "t1", "artist": "A", "album": "B", "title": "Song",
            "popularity_score": 90.0, "final_score": 90.0,
            "lastfm_listeners": 5000, "listenbrainz_listens": 4000,
            "lb_percentile": 0.95, "lastfm_score": 8.0, "listenbrainz_score": 9.0,
            "is_single": False, "single_confidence": "low",
            "single_sources": "", "is_live": False, "popularity_marked": False,
        }
        track.update(overrides)
        return track

    def _listener_counts(self):
        return [100, 200, 300, 500, 5000], [200, 300, 400, 500, 4000]

    def test_album_top_without_standout_proof_is_four_star(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # Top of the album (and catalogue) but no popularity-standout proof
        # (no top-10% marking, no popularity_z_standout source) → 4★, the top
        # of the album-percentile band. Popularity alone never reaches 5★.
        track = self._base_track()
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 4
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=False) == 4

    def test_popularity_marked_standout_gets_five_star(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # A genuine triple-standout (album z + artist z + artist top-10%
        # marking) reaches 5★ in both popularity-only and full scans.
        track = self._base_track(popularity_marked=True)
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 5
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=False) == 5

    def test_single_status_does_not_inflate_popularity_only_rating(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # A high-confidence single with only average popularity rates on its
        # popularity alone (2★) in a popularity-only pass — single status is
        # not a rating input because detection didn't run this pass. The full
        # scan honours the confirmed single and grants 5★.
        track = self._base_track(
            title="Mid Single",
            popularity_score=54.0,
            final_score=54.0,
            is_single=True,
            single_confidence="high",
            lastfm_listeners=300,
            listenbrainz_listens=200,
            lb_percentile=0.4,
        )
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 2
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=False) == 5

    def test_live_track_caps_at_four_in_popularity_only(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # Live recordings never reach 5★ from popularity alone (legacy parity).
        track = self._base_track(is_live=True)
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) <= 4

    def test_user_override_survives_popularity_only(self):
        from services.popularity.stages import finalise_stage as fs

        lf, lb = self._listener_counts()
        # A manually-set single is an explicit user preference — preserved by
        # every scan type, including popularity-only passes.
        track = self._base_track(single_confidence="user", popularity_score=10.0, final_score=10.0)
        assert fs._assign_stars(track, self._album_scores(), self._artist_scores(), lf, lb,
                                popularity_only=True) == 5


class TestComputeArtistScoresExcludesScanned:
    """Artist-wide score merge must not double-count this scan's tracks.

    Tracks scored during the current scan were persisted to the DB, so a naive
    merge (scan scores + ALL stored final_scores) double-counts them and
    drifts the artist z-scores. The album-level merge already excludes scanned
    titles; ``compute_artist_scores`` must do the same.
    """

    def test_scanned_titles_excluded_from_db_merge(self):
        from services.popularity.stages import finalise_stage as fs

        class _Cursor:
            def __init__(self):
                self.fetched = [
                    {"title": "Scanned A", "final_score": 80.0},
                    {"title": "Scanned B", "final_score": 90.0},
                    {"title": "Older Track", "final_score": 70.0},
                ]

            def execute(self, sql, params=None):
                self.sql = sql
                self.params = params

            def fetchall(self):
                return self.fetched

        cursor = _Cursor()
        # Both scanned titles must be excluded → only the older track anchors.
        scores = fs.compute_artist_scores(
            "Artist",
            [80.0, 90.0],
            object(),
            cursor,
            scanned_titles={"scanned a", "scanned b"},
        )
        assert scores == [80.0, 90.0, 70.0]


class TestFinaliseScanPerAlbumFlag:
    def _fegefeuer_results(self):
        # One album, three tracks that carry different TRACK artists (a
        # featured-artist split).  They must all group under the album artist
        # "Feuerschwanz", not fragment into three 1-track "albums".
        return [
            {
                "track_id": f"t{i}",
                "artist": artist,
                "album_artist": "Feuerschwanz",
                "album": "Fegefeuer",
                "title": f"Song {i}",
                "popularity_score": 60.0,
                "final_score": 60.0,
                "lastfm_listeners": 100,
                "listenbrainz_listens": 200,
                "lastfm_score": 4.0,
                "listenbrainz_score": 4.0,
                "is_single": False,
                "single_confidence": "low",
                "single_sources": "",
            }
            for i, artist in enumerate(
                ["Feuerschwanz", "Feuerschwanz feat. Fabienne Erni", "Feuerschwanz feat. Melissa Bonny"]
            )
        ]

    def test_featured_tracks_group_by_album_artist(self, monkeypatch):
        from services.popularity.stages import finalise_stage as fs

        conn = FakeConn()
        monkeypatch.setattr(fs, "get_db_connection", lambda: conn)
        posted = []
        monkeypatch.setattr(
            fs,
            "post_album_star_ratings",
            lambda **kw: posted.append(kw) or {"star_ratings": 3, "navidrome_synced": 0},
        )
        monkeypatch.setattr(fs, "_create_essential_m3u", lambda artist, cursor: None)
        monkeypatch.setattr(fs, "log_unified", lambda msg: None)

        fs.finalise_scan(
            results=self._fegefeuer_results(),
            options={"sync_navidrome": True},
        )

        # Exactly ONE album posting under the album artist — the feat. tracks
        # must not split into separate 1-track albums (tracks=1, MAD=0.0 →
        # broken z-scores / duplicate Navidrome syncs).
        assert len(posted) == 1
        assert posted[0]["artist"] == "Feuerschwanz"
        assert len(posted[0]["album_results"]) == 3
        assert posted[0]["album_results"][0]["album"] == "Fegefeuer"

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
        monkeypatch.setattr(fs, "_create_essential_m3u", lambda artist, cursor: None)
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
        monkeypatch.setattr(fs, "_create_essential_m3u", lambda artist, cursor: None)
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

        def flaky_assign(track, *args, **kwargs):
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
        assert any("📊 SCAN RESULTS: Muse — Absolution" in m for m in logged)
