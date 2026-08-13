"""Regression tests for issues observed in a live forced artist-scan log.

Covers:
- ``get_cached_popularity_for_titles`` must read SQLAlchemy 2.0 ``Row``
  objects via ``_mapping`` — string indexing (``row["title"]``) raises
  ``TypeError: tuple indices must be integers or slices, not str``, which
  made EVERY bulk cache read return ``{}`` ("bulk get failed for ...").
- ``convert_row_to_json_serializable`` must convert Jinja2 ``Undefined``
  values to ``None`` so ``json.dumps`` no longer raises "Object of type
  Undefined is not JSON serializable".
- ``lookup_artist_id`` must never return a name-keyed ``artist_stats`` row
  (an earlier ``finalise_stage`` wrote the artist NAME into the ``artist_id``
  PRIMARY KEY, so the Navidrome import called ``getArtist?id=<name>``,
  returned no albums, and skipped the import).
- ``_resolve_navidrome_artist_id`` reuses an existing real Navidrome id
  instead of corrupting ``artist_stats.artist_id`` with the artist name.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sa_text


# ---------------------------------------------------------------------------
# Helpers: real SQLAlchemy Row objects backed by an in-memory sqlite engine
# ---------------------------------------------------------------------------

_CACHE_COLUMNS = (
    "artist", "title", "lastfm_listeners", "lastfm_playcount",
    "listenbrainz_listens", "listenbrainz_users", "lastfm_tags", "updated_at",
)


def _make_rows(rows_data: list[tuple], *, single_col: str | None = None) -> list:
    """Return real SQLAlchemy ``Row`` objects for the relevant SELECT.

    ``rows_data`` is a list of value-tuples. When ``single_col`` is set, each
    tuple is a single value inserted into that one column (dict-form binds
    keep SQLAlchemy from mis-reading a 1-element tuple as a scalar).
    """
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        if single_col:
            conn.execute(sa_text(f"CREATE TABLE t ({single_col} TEXT)"))
            for (value,) in rows_data:
                conn.execute(
                    sa_text(f"INSERT INTO t ({single_col}) VALUES (:v)"),
                    {"v": value},
                )
            result = conn.execute(sa_text(f"SELECT {single_col} FROM t"))
            return list(result.fetchall())
        conn.execute(sa_text(
            "CREATE TABLE t (artist TEXT, title TEXT, lastfm_listeners INT, "
            "lastfm_playcount INT, listenbrainz_listens INT, "
            "listenbrainz_users INT, lastfm_tags TEXT, updated_at TEXT)"
        ))
        for r in rows_data:
            conn.execute(
                sa_text(
                    "INSERT INTO t (artist, title, lastfm_listeners, lastfm_playcount, "
                    "listenbrainz_listens, listenbrainz_users, lastfm_tags, updated_at) "
                    "VALUES (:artist, :title, :ll, :lp, :lb_l, :lb_u, :tags, :updated)"
                ),
                {
                    "artist": r[0], "title": r[1], "ll": r[2], "lp": r[3],
                    "lb_l": r[4], "lb_u": r[5], "tags": r[6], "updated": r[7],
                },
            )
        result = conn.execute(sa_text(
            f"SELECT {', '.join(_CACHE_COLUMNS)} FROM t"
        ))
        return list(result.fetchall())


def _make_artist_rows(rows_data: list[tuple[str, str]]) -> list:
    """Real SQLAlchemy Rows for ``(artist_id, artist_name)`` (artist_stats)."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa_text("CREATE TABLE t (artist_id TEXT, artist_name TEXT)"))
        for artist_id, artist_name in rows_data:
            conn.execute(
                sa_text("INSERT INTO t (artist_id, artist_name) VALUES (:id, :name)"),
                {"id": artist_id, "name": artist_name},
            )
        result = conn.execute(sa_text("SELECT artist_id, artist_name FROM t"))
        return list(result.fetchall())


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Stub SQLAlchemy session that returns a fixed set of rows per execute."""

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

    def close(self):
        pass


@contextmanager
def _fake_db_session(rows_by_execute):
    yield _FakeSession(rows_by_execute)


def _session_factory(rows_by_execute):
    """Return a ``db_session`` callable backed by ONE shared fake session.

    The real ``db_session()`` yields a fresh session per call but talks to the
    same DB, so sequential ``execute`` calls across separate ``with`` blocks
    advance one shared result queue.  Reusing the same ``_FakeSession``
    instance mirrors that so the second query in a function sees the second
    result set.
    """
    session = _FakeSession(rows_by_execute)

    @contextmanager
    def _cm():
        yield session

    return _cm


class TestCachedPopularityForTitles:
    """get_cached_popularity_for_titles must work with SQLAlchemy 2.0 Rows."""

    def test_returns_rows_keyed_by_lowercase_title(self, monkeypatch):
        import db.repositories.popularity_cache as cache

        rows = _make_rows([
            ("Ad Infinitum", "My Halo", 1000, 5000, 200, 150, '["symphonic","metal"]', "2026-08-01"),
            ("Ad Infinitum", "Herzblut (feat. Melissa Bonny)", 900, 4000, 100, 80, None, "2026-08-01"),
        ])
        monkeypatch.setattr(cache, "db_session", _session_factory([rows]))

        out = cache.get_cached_popularity_for_titles("Ad Infinitum", ["My Halo"])

        # The bulk read must NOT return {} — this regressed because
        # ``row["title"]`` raised ``tuple indices must be integers...``.
        assert out, "bulk cache read must return cached rows"
        assert "my halo" in out
        assert "herzblut (feat. melissa bonny)" in out
        assert out["my halo"]["lastfm_listeners"] == 1000
        assert out["my halo"]["title"] == "My Halo"
        assert out["my halo"]["lastfm_tags"] == '["symphonic","metal"]'

    def test_empty_rows_return_empty(self, monkeypatch):
        import db.repositories.popularity_cache as cache

        monkeypatch.setattr(cache, "db_session", _session_factory([[]]))
        assert cache.get_cached_popularity_for_titles("Ad Infinitum", ["X"]) == {}

    def test_null_title_rows_skipped(self, monkeypatch):
        import db.repositories.popularity_cache as cache

        rows = _make_rows([
            ("Ad Infinitum", None, 0, 0, 0, 0, None, None),
            ("Ad Infinitum", "Real", 500, 1000, 50, 20, None, "2026-08-01"),
        ])
        monkeypatch.setattr(cache, "db_session", _session_factory([rows]))

        out = cache.get_cached_popularity_for_titles("Ad Infinitum", ["Real"])
        assert "real" in out
        assert out["real"]["lastfm_listeners"] == 500


class TestConvertRowToJsonSerializableUndefined:
    """Jinja2 Undefined values must not break JSON serialization."""

    def test_undefined_becomes_none(self):
        from jinja2 import Undefined

        from db.utils import convert_row_to_json_serializable

        out = convert_row_to_json_serializable({"title": Undefined()})
        assert out == {"title": None}

    def test_nested_undefined_becomes_none(self):
        from jinja2 import Undefined

        from db.utils import convert_row_to_json_serializable

        out = convert_row_to_json_serializable({"track": {"x": Undefined()}, "n": 1})
        assert out == {"track": {"x": None}, "n": 1}

    def test_json_dumps_succeeds(self):
        import json

        from jinja2 import Undefined

        from db.utils import convert_row_to_json_serializable

        payload = convert_row_to_json_serializable({"track": Undefined(), "ok": True})
        # json.dumps must not raise "Object of type Undefined is not JSON serializable".
        assert json.loads(json.dumps(payload)) == {"track": None, "ok": True}


class TestLookupArtistIdSkipsNameKeyedRows:
    """lookup_artist_id must never return the artist NAME as an id."""

    def test_returns_none_when_only_name_keyed_row_exists(self, monkeypatch):
        from db.repositories import scan_repository

        # artist_stats stores artist_id = "Ad Infinitum" (the name) — this is
        # the corruption introduced by finalise_stage writing name-as-id.
        name_row = _make_rows([("Ad Infinitum",)], single_col="artist_id")
        monkeypatch.setattr(
            scan_repository, "db_session",
            lambda: _fake_db_session([name_row, name_row]),
        )

        assert scan_repository.lookup_artist_id("Ad Infinitum") is None

    def test_returns_real_id_when_present(self, monkeypatch):
        from db.repositories import scan_repository

        real_id = "nC8zJIGaf8CEiq8PT5L5cu"
        rows = _make_rows([(real_id,)], single_col="artist_id")
        monkeypatch.setattr(
            scan_repository, "db_session",
            _session_factory([rows]),
        )

        assert scan_repository.lookup_artist_id("Ad Infinitum") == real_id

    def test_variant_lookup_skips_name_keyed_rows(self, monkeypatch):
        from db.repositories import scan_repository

        # Exact match: no row. Variant pass returns only the name-keyed row.
        empty = _make_rows([], single_col="artist_id")
        name_rows = _make_artist_rows([("Ad Infinitum", "Ad Infinitum")])
        monkeypatch.setattr(
            scan_repository, "db_session",
            _session_factory([empty, name_rows]),
        )

        assert scan_repository.lookup_artist_id("Ad Infinitum") is None

    def test_variant_lookup_returns_real_id(self, monkeypatch):
        from db.repositories import scan_repository

        empty = _make_rows([], single_col="artist_id")
        real_id = "nC8zJIGaf8CEiq8PT5L5cu"
        rows = _make_artist_rows([(real_id, "Ad Infinitum")])
        monkeypatch.setattr(
            scan_repository, "db_session",
            _session_factory([empty, rows]),
        )

        assert scan_repository.lookup_artist_id("Ad Infinitum") == real_id


class TestResolveNavidromeArtistId:
    """finalise_stage must reuse a real id instead of writing the name."""

    def test_returns_existing_real_id_from_artist_stats(self, monkeypatch):
        from services.popularity.stages.finalise_stage import _resolve_navidrome_artist_id
        import db.engine as db_engine

        rows = _make_rows([("nC8zJIGaf8CEiq8PT5L5cu",)], single_col="artist_id")
        monkeypatch.setattr(db_engine, "db_session", _session_factory([rows]))

        assert _resolve_navidrome_artist_id("Ad Infinitum") == "nC8zJIGaf8CEiq8PT5L5cu"

    def test_skips_name_keyed_artist_stats_row(self, monkeypatch):
        from services.popularity.stages.finalise_stage import _resolve_navidrome_artist_id
        import db.engine as db_engine

        # artist_stats only has the corrupted name-keyed row — must NOT return it.
        name_rows = _make_rows([("Ad Infinitum",)], single_col="artist_id")
        real_rows = _make_rows([("nC8zJIGaf8CEiq8PT5L5cu",)], single_col="artist_id")
        monkeypatch.setattr(db_engine, "db_session", _session_factory([name_rows, real_rows]))

        assert _resolve_navidrome_artist_id("Ad Infinitum") == "nC8zJIGaf8CEiq8PT5L5cu"

    def test_returns_none_when_no_real_id_anywhere(self, monkeypatch):
        from services.popularity.stages.finalise_stage import _resolve_navidrome_artist_id
        import db.engine as db_engine

        empty = _make_rows([], single_col="artist_id")
        monkeypatch.setattr(db_engine, "db_session", _session_factory([empty, empty]))

        assert _resolve_navidrome_artist_id("Ad Infinitum") is None

    def test_ignores_name_in_tracks_fallback(self, monkeypatch):
        from services.popularity.stages.finalise_stage import _resolve_navidrome_artist_id
        import db.engine as db_engine

        empty = _make_rows([], single_col="artist_id")
        name_rows = _make_rows([("Ad Infinitum",)], single_col="artist_id")
        monkeypatch.setattr(db_engine, "db_session", _session_factory([empty, name_rows]))

        assert _resolve_navidrome_artist_id("Ad Infinitum") is None
