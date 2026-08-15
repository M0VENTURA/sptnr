"""Tests for the genre top-tracks playlist create/delete thresholds.

``_create_genre_top_track_playlists`` writes a ``{Genre} - Top Tracks.m3u``
for every genre whose qualifying (≥ Minimum Stars) track pool clears the
create threshold, and removes the file once the pool drops below the delete
threshold.  The gap between the two thresholds acts as hysteresis: a genre
between them keeps whatever playlist already exists.

Config keys (under ``playlists``):
- ``genre_playlists_enabled``           (default True)  – creation toggle
- ``genre_playlists_delete_enabled``    (default True)  – deletion toggle
- ``genre_playlists_create_threshold``  (default 100)   – create above
- ``genre_playlists_delete_threshold``  (default 80)    – delete below
- ``genre_playlists_name_template``     (default "{genre} - Top Tracks")
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sa_text

from services.popularity.stages import finalise_stage as fs


_GENRE_COLUMNS = (
    "id", "title", "file_path", "duration", "artist", "album_artist",
    "stars", "popularity_score", "is_live", "is_compilation",
    "lastfm_tags", "listenbrainz_genres", "discogs_genres",
    "musicbrainz_genres", "spotify_genres",
    "essentia_genres", "manual_genres", "navidrome_genres",
)


def _make_rows(rows_data: list[dict]) -> list:
    """Real SQLAlchemy ``Row`` objects shaped like the genre-playlist SELECT."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa_text(
            "CREATE TABLE t (id TEXT, title TEXT, file_path TEXT, duration INT, "
            "artist TEXT, album_artist TEXT, stars INT, popularity_score REAL, "
            "is_live INT, is_compilation INT, lastfm_tags TEXT, "
            "listenbrainz_genres TEXT, discogs_genres TEXT, musicbrainz_genres TEXT, "
            "spotify_genres TEXT, essentia_genres TEXT, manual_genres TEXT, "
            "navidrome_genres TEXT)"
        ))
        for r in rows_data:
            conn.execute(
                sa_text(
                    "INSERT INTO t (id, title, file_path, duration, artist, album_artist, "
                    "stars, popularity_score, is_live, is_compilation, lastfm_tags, "
                    "listenbrainz_genres, discogs_genres, musicbrainz_genres, "
                    "spotify_genres, essentia_genres, manual_genres, navidrome_genres) "
                    "VALUES (:id, :title, :file_path, :duration, :artist, :album_artist, "
                    ":stars, :popularity_score, :is_live, :is_compilation, :lastfm_tags, "
                    ":listenbrainz_genres, :discogs_genres, :musicbrainz_genres, "
                    ":spotify_genres, :essentia_genres, :manual_genres, :navidrome_genres)"
                ),
                {col: r.get(col) for col in _GENRE_COLUMNS},
            )
        return list(conn.execute(
            sa_text(f"SELECT {', '.join(_GENRE_COLUMNS)} FROM t")
        ).fetchall())


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows_by_execute):
        self._results = list(rows_by_execute)
        self._index = 0

    def execute(self, *args, **kwargs):
        rows = self._results[self._index] if self._index < len(self._results) else []
        self._index += 1
        return _FakeResult(rows)

    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def _fake_db_session(session):
    yield session


def _session_factory(session):
    @contextmanager
    def _cm():
        yield session

    return _cm


def make_row(title, stars=5, genre="Rock", artist="Artist", file_path=None):
    """One track row with a single ``navidrome_genres`` value."""
    return {
        "id": f"id-{genre}-{title}",
        "title": title,
        "file_path": file_path or f"/music/{artist}/{title}.flac",
        "duration": 180,
        "stars": stars,
        "popularity_score": 50.0,
        "is_live": 0,
        "is_compilation": 0,
        "artist": artist,
        "album_artist": artist,
        "lastfm_tags": None,
        "listenbrainz_genres": None,
        "discogs_genres": None,
        "musicbrainz_genres": None,
        "spotify_genres": None,
        "essentia_genres": None,
        "manual_genres": None,
        "navidrome_genres": genre,
    }


def make_cfg(**overrides) -> dict:
    cfg = {
        "genre_playlists_enabled": True,
        "genre_playlists_delete_enabled": True,
        "genre_playlists_create_threshold": 100,
        "genre_playlists_delete_threshold": 80,
        "genre_playlists_min_stars": 4,
        "genre_playlists_max_genres": 3,
        "genre_playlists_name_template": "{genre} - Top Tracks",
    }
    cfg.update(overrides)
    return {"playlists": cfg}


@pytest.fixture
def run_genre_playlists(tmp_path, monkeypatch):
    """Return a helper that runs the function against a temp Playlists dir."""
    playlists_dir = tmp_path / "Playlists"
    state_file = tmp_path / "genre_playlists.json"
    monkeypatch.setattr(fs, "_essential_playlists_dir", lambda: str(playlists_dir))
    monkeypatch.setattr(fs, "_genre_playlists_state_file", lambda: str(state_file))

    def _run(rows, cfg=None):
        from helpers import config_helpers
        import db.engine as db_engine
        monkeypatch.setattr(config_helpers, "get_config", lambda: cfg or make_cfg())
        session = _FakeSession([_make_rows(rows), []])
        monkeypatch.setattr(db_engine, "db_session", _session_factory(session))
        written = fs._create_genre_top_track_playlists()
        return written, playlists_dir

    return _run


def _path(playlists_dir, name):
    return playlists_dir / name


class TestCreateThreshold:
    def test_creates_when_over_100(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 102)]
        written, d = run_genre_playlists(rows)
        assert written == 1
        assert _path(d, "Rock - Top Tracks.m3u").exists()

    def test_does_not_create_at_exactly_100(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 101)]
        cfg = make_cfg(genre_playlists_create_threshold=100)
        written, d = run_genre_playlists(rows, cfg)
        assert written == 0
        assert not _path(d, "Rock - Top Tracks.m3u").exists()

    def test_respects_custom_create_threshold(self, run_genre_playlists):
        cfg = make_cfg(genre_playlists_create_threshold=40)
        rows = [make_row(f"Song {i}") for i in range(1, 40)]
        written, d = run_genre_playlists(rows, cfg)
        assert written == 0
        assert not _path(d, "Rock - Top Tracks.m3u").exists()

        rows2 = [make_row(f"Song {i}") for i in range(1, 42)]
        written2, d2 = run_genre_playlists(rows2, cfg)
        assert written2 == 1
        assert _path(d2, "Rock - Top Tracks.m3u").exists()


class TestDeleteThreshold:
    def test_deletes_when_below_80(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        written, d = run_genre_playlists(rows)
        assert written == 1
        target = _path(d, "Rock - Top Tracks.m3u")
        assert target.exists()

        fewer = [make_row(f"Song {i}") for i in range(1, 50)]
        written2, d2 = run_genre_playlists(fewer)
        assert written2 == 0
        assert not target.exists()

    def test_keeps_existing_playlist_between_thresholds(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        written, d = run_genre_playlists(rows)
        assert written == 1
        target = _path(d, "Rock - Top Tracks.m3u")
        assert target.exists()

        between = [make_row(f"Song {i}") for i in range(1, 91)]
        written2, d2 = run_genre_playlists(between)
        assert written2 == 0
        assert target.exists()

    def test_respects_custom_delete_threshold(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 61)]
        written, d = run_genre_playlists(rows)
        assert written == 0

        target = _path(d, "Rock - Top Tracks.m3u")
        target.write_text("#EXTM3U\n", encoding="utf-8")

        cfg = make_cfg(genre_playlists_delete_threshold=50)
        written2, d2 = run_genre_playlists(rows, cfg)
        assert written2 == 0
        assert target.exists()

        cfg2 = make_cfg(genre_playlists_delete_threshold=70)
        written3, d3 = run_genre_playlists(rows, cfg2)
        assert written3 == 0
        assert not target.exists()


class TestToggles:
    def test_create_disabled_does_not_create(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        cfg = make_cfg(genre_playlists_enabled=False)
        written, d = run_genre_playlists(rows, cfg)
        assert written == 0
        assert not _path(d, "Rock - Top Tracks.m3u").exists()

    def test_delete_disabled_keeps_stale_playlist(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        written, d = run_genre_playlists(rows)
        assert written == 1
        target = _path(d, "Rock - Top Tracks.m3u")
        assert target.exists()

        fewer = [make_row(f"Song {i}") for i in range(1, 10)]
        cfg = make_cfg(genre_playlists_delete_enabled=False)
        written2, d2 = run_genre_playlists(fewer, cfg)
        assert written2 == 0
        assert target.exists()

    def test_delete_only_still_cleans_up(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        written, d = run_genre_playlists(rows)
        assert written == 1
        target = _path(d, "Rock - Top Tracks.m3u")
        assert target.exists()

        fewer = [make_row(f"Song {i}") for i in range(1, 10)]
        cfg = make_cfg(genre_playlists_enabled=False, genre_playlists_delete_enabled=True)
        written2, d2 = run_genre_playlists(fewer, cfg)
        assert written2 == 0
        assert not target.exists()


class TestNameTemplate:
    def test_uses_config_name_template(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        cfg = make_cfg(genre_playlists_name_template="{genre} Mix")
        written, d = run_genre_playlists(rows, cfg)
        assert written == 1
        assert _path(d, "Rock Mix.m3u").exists()

    def test_template_change_removes_old_file(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        written, d = run_genre_playlists(rows)
        old = _path(d, "Rock - Top Tracks.m3u")
        assert old.exists()

        cfg = make_cfg(genre_playlists_name_template="{genre} Mix")
        written2, d2 = run_genre_playlists(rows, cfg)
        new = _path(d2, "Rock Mix.m3u")
        assert new.exists()
        assert not old.exists()


class TestEdgeCases:
    def test_other_playlists_untouched(self, run_genre_playlists):
        rows = [make_row(f"Song {i}") for i in range(1, 121)]
        written, d = run_genre_playlists(rows)
        assert written == 1

        unrelated = _path(d, "My Unrelated Mix.m3u")
        unrelated.write_text("#EXTM3U\n", encoding="utf-8")

        fewer = [make_row(f"Song {i}") for i in range(1, 10)]
        run_genre_playlists(fewer)

        assert unrelated.exists()
        assert not _path(d, "Rock - Top Tracks.m3u").exists()

    def test_dedup_counts_once_for_threshold(self, run_genre_playlists):
        # 150 rows but only 60 unique (artist, title) groups — below 100.
        rows = []
        for i in range(1, 61):
            rows.append(make_row(f"Song {i}", artist="A"))
            rows.append(make_row(f"Song {i} (Live)", artist="A"))
            rows.append(make_row(f"Song {i} [Remaster]", artist="A"))
        written, d = run_genre_playlists(rows)
        assert written == 0
        assert not _path(d, "Rock - Top Tracks.m3u").exists()

    def test_dedup_by_track_artist_across_album_artists(self, run_genre_playlists):
        # The same song on the artist's own album and on a compilation has a
        # different album_artist — dedup must key on the TRACK artist, so the
        # song counts once. 360 rows / 3 versions = 120 unique groups (over
        # the 100 create threshold) and the playlist holds exactly 120 tracks.
        rows = []
        for i in range(1, 121):
            rows.append({**make_row(f"Song {i}", artist="Alterium")})
            rows.append({
                **make_row(f"Song {i} (Live)", artist="Alterium"),
                "is_live": 1,
            })
            rows.append({
                **make_row(f"Song {i} [Remaster]", artist="Alterium"),
                "album_artist": "Various Artists",
                "is_compilation": 1,
            })
        written, d = run_genre_playlists(rows)
        assert written == 1
        target = _path(d, "Rock - Top Tracks.m3u")
        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert content.count("#EXTINF") == 120

    def test_db_fetch_failure_is_graceful(self, monkeypatch):
        import db.engine as db_engine

        class BoomSession:
            def execute(self, sql, params=None):
                raise RuntimeError("db down")

            def commit(self):
                pass

            def rollback(self):
                pass

        monkeypatch.setattr(db_engine, "db_session", _session_factory(BoomSession()))
        written = fs._create_genre_top_track_playlists()
        assert written == 0

    def test_multi_genre_pools(self, run_genre_playlists):
        rows = [make_row(f"Song {i}", genre="Rock") for i in range(1, 121)]
        rows += [make_row(f"Song {i}", genre="Metal") for i in range(1, 121)]
        written, d = run_genre_playlists(rows)
        assert written == 2
        assert _path(d, "Rock - Top Tracks.m3u").exists()
        assert _path(d, "Metal - Top Tracks.m3u").exists()


class _FakeNavidromeClient:
    """Minimal Navidrome client stand-in for the orphan-sweep tests."""

    def __init__(self, playlists):
        self._playlists = playlists
        self.deleted = []

    def fetch_all_playlists(self):
        return list(self._playlists)

    def find_playlist_by_name(self, name):
        wanted = str(name or "").strip().lower()
        for p in self._playlists:
            if str(p.get("name") or "").strip().lower() == wanted:
                return p
        return None

    def delete_playlist(self, playlist_id):
        self.deleted.append(playlist_id)
        return True


class TestNavidromeOrphanSweep:
    """Navidrome keeps imported genre playlists even after the ``.m3u`` file
    is removed — the self-healing sweep must delete exactly the ones whose
    file is gone (the reported "removed from disk but still on Navidrome")."""

    def test_sweeps_orphaned_keeps_present_and_foreign(self, tmp_path, monkeypatch):
        playlists_dir = tmp_path / "Playlists"
        playlists_dir.mkdir(parents=True, exist_ok=True)
        # A genre playlist file that still exists on disk must be kept.
        (playlists_dir / "Alternative - Top Tracks.m3u").write_text("#EXTM3U\n")
        monkeypatch.setattr(fs, "_essential_playlists_dir", lambda: str(playlists_dir))
        monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: True)

        client = _FakeNavidromeClient([
            {"id": "p1", "name": "Alt-rock - Top Tracks"},                    # no file → sweep
            {"id": "p2", "name": "Alternative - Top Tracks"},                 # file exists → keep
            {"id": "p3", "name": "Amon Amarth - Essential Collection"},       # not genre suffix → keep
        ])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs._sweep_orphaned_genre_playlists_from_navidrome()

        assert client.deleted == ["p1"]

    def test_respects_delete_toggle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs, "_genre_playlists_delete_enabled", lambda: False)
        client = _FakeNavidromeClient([{"id": "p1", "name": "Alt-rock - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs._sweep_orphaned_genre_playlists_from_navidrome()

        assert client.deleted == []

    def test_delete_by_name_uses_normalized_clients(self, monkeypatch):
        """_delete_genre_playlist_from_navidrome must resolve clients via
        _navidrome_clients (normalised config) and delete by case-insensitive
        name."""
        client = _FakeNavidromeClient([{"id": "p9", "name": "Alt-rock - Top Tracks"}])
        monkeypatch.setattr(fs, "_navidrome_clients", lambda: [client])

        fs._delete_genre_playlist_from_navidrome("Alt-Rock - Top Tracks")

        assert client.deleted == ["p9"]


class TestPopularityOrderingAndNoCap:
    """Genre playlists are sorted by popularity (most popular first) and
    include EVERY qualifying track — there is no top-N cap anymore."""

    def _titles(self, content: str) -> list[str]:
        titles = []
        for line in content.splitlines():
            if line.startswith("#EXTINF:"):
                titles.append(line.split(",", 1)[1].split(" - ", 1)[1])
        return titles

    def test_sorted_by_popularity_desc(self, run_genre_playlists):
        rows = []
        for i in range(1, 121):
            rows.append({**make_row(f"Song {i}"), "popularity_score": float(i)})
        written, d = run_genre_playlists(rows)
        assert written == 1
        content = _path(d, "Rock - Top Tracks.m3u").read_text(encoding="utf-8")
        titles = self._titles(content)
        assert titles == [f"Song {i}" for i in range(120, 0, -1)]

    def test_every_qualifying_track_included_no_cap(self, run_genre_playlists):
        # 700 qualifying tracks — the old top-N cap (500) would truncate.
        rows = [make_row(f"Song {i}") for i in range(1, 701)]
        written, d = run_genre_playlists(rows)
        assert written == 1
        content = _path(d, "Rock - Top Tracks.m3u").read_text(encoding="utf-8")
        assert content.count("#EXTINF") == 700

    def test_lower_star_but_popular_track_ranks_first(self, run_genre_playlists):
        # Popularity is the primary playlist key: a lower-starred but hugely
        # popular track outranks a higher-starred quiet track.
        rows = [
            {**make_row("Popular 4 Star"), "stars": 4, "popularity_score": 95.0},
            {**make_row("Quiet 5 Star"), "stars": 5, "popularity_score": 40.0},
        ]
        rows += [
            {**make_row(f"Filler {i}"), "stars": 4, "popularity_score": 30.0}
            for i in range(1, 119)
        ]
        written, d = run_genre_playlists(rows)
        assert written == 1
        content = _path(d, "Rock - Top Tracks.m3u").read_text(encoding="utf-8")
        titles = self._titles(content)
        assert titles[0] == "Popular 4 Star"
        assert titles[1] == "Quiet 5 Star"
