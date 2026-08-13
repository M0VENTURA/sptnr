"""Tests for the Essential Collection .m3u generation.

The popularity scan used to write Navidrome Smart Playlist (``.nsp``) files
whenever an artist had 10+ five-star tracks.  It now writes a deduplicated
``[Album Artist] - Essential Collection.m3u`` into the watch Playlists
directory instead, using the full DB track history scoped to ``album_artist``:

- created/refreshed only when an artist has MORE than 12 unique 4★/5★ tracks
- grouped by normalized title (parenthetical/bracket noise stripped) with a
  deterministic winner: studio over live → main discography over compilation
  → higher rating → higher popularity → earlier year
- deleted (plus stale NSP files) when the unique count drops to 12 or below
- compilation/sampler buckets (Various Artists, Soundtrack, VA, ...) skipped
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sa_text

from services.popularity.stages import finalise_stage as fs


# ---------------------------------------------------------------------------
# Helpers: route finalise_stage's db_session to real SQLAlchemy Rows
# ---------------------------------------------------------------------------

_ESSENTIAL_COLUMNS = (
    "id", "title", "file_path", "duration", "stars", "is_live",
    "is_compilation", "popularity_score", "year", "release_year", "artist",
)


def _make_rows(rows_data: list[dict]) -> list:
    """Real SQLAlchemy ``Row`` objects shaped like the essential SELECT."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa_text(
            "CREATE TABLE t (id TEXT, title TEXT, file_path TEXT, duration INT, "
            "stars INT, is_live INT, is_compilation INT, popularity_score REAL, "
            "year INT, release_year INT, artist TEXT)"
        ))
        for r in rows_data:
            conn.execute(
                sa_text(
                    "INSERT INTO t (id, title, file_path, duration, stars, is_live, "
                    "is_compilation, popularity_score, year, release_year, artist) "
                    "VALUES (:id, :title, :fp, :dur, :stars, :live, :comp, :score, "
                    ":year, :ryear, :artist)"
                ),
                {
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "fp": r.get("file_path"),
                    "dur": r.get("duration"),
                    "stars": r.get("stars"),
                    "live": r.get("is_live"),
                    "comp": r.get("is_compilation"),
                    "score": r.get("popularity_score"),
                    "year": r.get("year"),
                    "ryear": r.get("release_year"),
                    "artist": r.get("artist", ""),
                },
            )
        return list(conn.execute(
            sa_text(f"SELECT {', '.join(_ESSENTIAL_COLUMNS)} FROM t")
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


def _patch_db(monkeypatch, rows: list[dict]) -> None:
    """Route finalise_stage's ``db_session`` to a fake returning *rows*."""
    import db.engine as db_engine

    session = _FakeSession([_make_rows(rows), []])
    monkeypatch.setattr(db_engine, "db_session", _session_factory(session))


def make_row(
    title,
    stars=5,
    is_live=0,
    is_compilation=0,
    popularity_score=50.0,
    year=2020,
    release_year=None,
    file_path="/music/Artist/Album/Song.flac",
    duration=180,
):
    return {
        "id": f"id-{title}",
        "title": title,
        "file_path": file_path,
        "duration": duration,
        "stars": stars,
        "is_live": is_live,
        "is_compilation": is_compilation,
        "popularity_score": popularity_score,
        "year": year,
        "release_year": release_year,
    }


def _unique_titles(count):
    return [f"Song {i}" for i in range(1, count + 1)]


class TestTitleNormalization:
    def test_strips_parenthetical_and_bracket_noise(self):
        assert fs._normalise_essential_title("Walk (2018 Remaster)") == "walk"
        assert fs._normalise_essential_title("Play [Deluxe Edition]") == "play"
        assert fs._normalise_essential_title("Alive (Live)") == "alive"
        assert fs._normalise_essential_title("Foo (Version) Bar") == "foo bar"

    def test_collapses_whitespace_and_case(self):
        assert fs._normalise_essential_title("  The   Song  ") == "the song"

    def test_empty_title(self):
        assert fs._normalise_essential_title("") == ""
        assert fs._normalise_essential_title(None) == ""


class TestExcludedArtists:
    @pytest.mark.parametrize(
        "artist",
        ["Various Artists", "various artists", "Soundtrack", "Soundtracks",
         "VA", "V/A", "va", "Unknown Artist", "unknown"],
    )
    def test_compilation_buckets_excluded(self, artist):
        assert fs._is_excluded_essential_artist(artist)

    def test_normal_artist_included(self):
        assert not fs._is_excluded_essential_artist("Poppy")
        assert not fs._is_excluded_essential_artist("Lord of the Lost")


class TestCreateEssentialM3u:
    def _run(self, tmp_path, artist="Poppy", rows=None, monkeypatch=None):
        playlists_dir = tmp_path / "Playlists"
        _patch_db(monkeypatch, rows or [])
        monkeypatch.setattr(fs, "_essential_playlists_dir", lambda: str(playlists_dir))
        fs._create_essential_m3u(artist)
        return playlists_dir

    def test_creates_m3u_with_13_unique_tracks(self, tmp_path, monkeypatch):
        playlists_dir = self._run(
            tmp_path,
            rows=[make_row(t) for t in _unique_titles(13)],
            monkeypatch=monkeypatch,
        )
        m3u = playlists_dir / "Poppy - Essential Collection.m3u"
        assert m3u.exists()
        content = m3u.read_text(encoding="utf-8").splitlines()
        assert content[0] == "#EXTM3U"
        assert len(content) == 1 + 13 * 2
        assert "#EXTINF:180,Poppy - Song 1" in content
        assert "/music/Artist/Album/Song.flac" in content

    def test_uses_config_name_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            fs,
            "_essential_playlist_name",
            lambda artist: f"{artist} - Essentials",
        )
        playlists_dir = self._run(
            tmp_path,
            rows=[make_row(t) for t in _unique_titles(13)],
            monkeypatch=monkeypatch,
        )
        assert (playlists_dir / "Poppy - Essentials.m3u").exists()

    def test_below_threshold_deletes_and_does_not_create(self, tmp_path, monkeypatch):
        playlists_dir = self._run(
            tmp_path,
            rows=[make_row(t) for t in _unique_titles(12)],
            monkeypatch=monkeypatch,
        )
        m3u = playlists_dir / "Poppy - Essential Collection.m3u"
        assert not m3u.exists()
        # A stale file from a previous scan is removed.
        playlists_dir.mkdir(parents=True, exist_ok=True)
        m3u.write_text("#EXTM3U\n", encoding="utf-8")
        _patch_db(monkeypatch, [make_row(t) for t in _unique_titles(12)])
        fs._create_essential_m3u("Poppy")
        assert not m3u.exists()

    def test_removes_stale_nsp_files_on_write(self, tmp_path, monkeypatch):
        playlists_dir = self._run(
            tmp_path,
            rows=[make_row(t) for t in _unique_titles(13)],
            monkeypatch=monkeypatch,
        )
        legacy = playlists_dir / "Poppy Essential Playlist.nsp"
        old_name = playlists_dir / "Poppy - Essential Collection.nsp"
        legacy.write_text("{}", encoding="utf-8")
        old_name.write_text("{}", encoding="utf-8")
        _patch_db(monkeypatch, [make_row(t) for t in _unique_titles(13)])
        fs._create_essential_m3u("Poppy")
        assert not legacy.exists()
        assert not old_name.exists()

    def test_excluded_artist_is_noop(self, tmp_path, monkeypatch):
        playlists_dir = self._run(
            tmp_path,
            artist="Various Artists",
            rows=[make_row(t) for t in _unique_titles(13)],
            monkeypatch=monkeypatch,
        )
        assert not (playlists_dir / "Various Artists - Essential Collection.m3u").exists()

    def test_db_fetch_failure_is_graceful(self, tmp_path, monkeypatch):
        import db.engine as db_engine

        class BoomSession:
            def execute(self, *args, **kwargs):
                raise RuntimeError("db down")

            def commit(self):
                pass

            def rollback(self):
                pass

        monkeypatch.setattr(db_engine, "db_session", _session_factory(BoomSession()))
        playlists_dir = tmp_path / "Playlists"
        monkeypatch.setattr(fs, "_essential_playlists_dir", lambda: str(playlists_dir))
        fs._create_essential_m3u("Poppy")  # must not raise
        assert not (playlists_dir / "Poppy - Essential Collection.m3u").exists()


class TestDedupWinner:
    def _write(self, tmp_path, rows, monkeypatch):
        playlists_dir = tmp_path / "Playlists"
        _patch_db(monkeypatch, rows)
        monkeypatch.setattr(fs, "_essential_playlists_dir", lambda: str(playlists_dir))
        fs._create_essential_m3u("Poppy")
        m3u = playlists_dir / "Poppy - Essential Collection.m3u"
        assert m3u.exists(), "dedup test needs >12 unique groups"
        return m3u.read_text(encoding="utf-8")

    def test_studio_over_live(self, tmp_path, monkeypatch):
        # The studio cut is only 4★ but still beats the 5★ live take
        # (priority 1: is_live ASC, before rating).
        rows = [
            make_row("Song (Live)", stars=5, is_live=1, popularity_score=90.0,
                     file_path="/music/Poppy/Song Live.flac"),
            make_row("Song", stars=4, is_live=0, popularity_score=80.0,
                     file_path="/music/Poppy/Song.flac"),
        ] + [make_row(t) for t in _unique_titles(12)]
        content = self._write(tmp_path, rows, monkeypatch)
        assert "Song.flac" in content
        assert "Song Live.flac" not in content

    def test_main_discography_over_compilation(self, tmp_path, monkeypatch):
        # Priority 2: is_compilation ASC — the studio/remaster cut wins over
        # the greatest-hits compilation regardless of rating.
        rows = [
            make_row("Best Hit", stars=5, is_compilation=1, popularity_score=95.0,
                     file_path="/music/Poppy/Greatest Hits/Best Hit.flac"),
            make_row("Best Hit (2018 Remaster)", stars=4, is_compilation=0, popularity_score=60.0,
                     file_path="/music/Poppy/Album/Best Hit (2018 Remaster).flac"),
        ] + [make_row(t) for t in _unique_titles(12)]
        content = self._write(tmp_path, rows, monkeypatch)
        assert "Best Hit (2018 Remaster).flac" in content
        assert "Greatest Hits/Best Hit.flac" not in content

    def test_rating_beats_popularity(self, tmp_path, monkeypatch):
        # Priority 3: star_rating DESC — a 5★ original beats a 4★ deluxe cut
        # even though the deluxe version is more popular.
        rows = [
            make_row("Anthem [Deluxe Edition]", stars=4, popularity_score=99.0,
                     file_path="/music/Poppy/Deluxe/Anthem Deluxe.flac"),
            make_row("Anthem", stars=5, popularity_score=50.0,
                     file_path="/music/Poppy/Anthem.flac"),
        ] + [make_row(t) for t in _unique_titles(12)]
        content = self._write(tmp_path, rows, monkeypatch)
        assert "Anthem.flac" in content
        assert "Anthem Deluxe.flac" not in content

    def test_popularity_tiebreak(self, tmp_path, monkeypatch):
        # Priority 4: popularity_score DESC — equal rating/live/compilation,
        # so the higher-popularity original wins.
        rows = [
            make_row("Banger (2008 Remaster)", stars=5, popularity_score=55.0,
                     file_path="/music/Poppy/Banger Remaster.flac"),
            make_row("Banger", stars=5, popularity_score=70.0,
                     file_path="/music/Poppy/Banger.flac"),
        ] + [make_row(t) for t in _unique_titles(12)]
        content = self._write(tmp_path, rows, monkeypatch)
        assert "Banger.flac" in content
        assert "Banger Remaster.flac" not in content

    def test_earlier_year_wins(self, tmp_path, monkeypatch):
        # Priority 5: year ASC — with everything else tied, the original
        # release year beats a later remaster/re-issue.
        rows = [
            make_row("Classic (2021 Remaster)", stars=5, popularity_score=95.0, year=2021,
                     file_path="/music/Poppy/Classic Remaster.flac"),
            make_row("Classic", stars=5, popularity_score=95.0, year=1990,
                     file_path="/music/Poppy/Classic.flac"),
        ] + [make_row(t) for t in _unique_titles(12)]
        content = self._write(tmp_path, rows, monkeypatch)
        assert "Classic.flac" in content
        assert "Classic Remaster.flac" not in content
