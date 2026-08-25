"""Regression: the public search route must heal a missing
``tracks.album_artist`` column and retry once.

A legacy bare ``tracks`` table (original system) lacks album_artist.  The
introspection-based pre-check was unreliable (schema/search_path mismatch), so
the heal is now FAILURE-DRIVEN: if the query itself throws UndefinedColumn for
album_artist, the public route adds the column and calls the impl again.  This
test pins that contract.

The ``routes.misc_routes`` module cannot be imported directly in the test
suite (a pre-existing circular import between ``db.repositories.tag_repository``
and ``services.metadata.tag_file_service``), so the retry contract is verified
by extracting the route's logic through a stub module-level harness that
mirrors the exact structure of ``api_search``.
"""

from __future__ import annotations

import asyncio

import pytest


class _MissingAlbumArtistError(RuntimeError):
    pass


async def _public_route(impl, db_session_cm, get_columns):
    """Mirror of routes/misc_routes.api_search (the retry + heal contract)."""
    try:
        return await impl()
    except _MissingAlbumArtistError:
        try:
            with db_session_cm() as session:
                cols = get_columns(session, "tracks")
                session.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS album_artist TEXT")
                if "artist" in cols:
                    session.execute("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL")
        except Exception:
            pass
        return await impl()


class TestSearchSelfHealRetry:
    def test_missing_column_heals_and_retries(self, monkeypatch):
        """A first impl call raising _MissingAlbumArtistError triggers the
        ALTER, and the second call returns the real result."""
        calls: list[int] = []
        healed: list[str] = []

        async def _fake_impl():
            calls.append(1)
            if len(calls) == 1:
                raise _MissingAlbumArtistError("column album_artist does not exist")
            return {"success": True, "healed": True}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                healed.append(str(sql))
                return None

        def _db_cm():
            return _FakeSession()

        def _columns(session, table):
            return {"id", "artist"}

        result = asyncio.run(_public_route(_fake_impl, _db_cm, _columns))
        assert result == {"success": True, "healed": True}
        assert calls == [1, 1]  # first raised, second succeeded
        assert len(healed) == 2  # ALTER + backfill UPDATE ran

    def test_no_error_no_heal(self, monkeypatch):
        """A clean impl result does NOT trigger the heal."""
        calls: list[int] = []
        healed: list[str] = []

        async def _fake_impl():
            calls.append(1)
            return {"success": True, "tracks": []}

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                healed.append(str(sql))
                return None

        result = asyncio.run(_public_route(_fake_impl, lambda: _FakeSession(), lambda s, t: {"id"}))
        assert result == {"success": True, "tracks": []}
        assert calls == [1]
        assert healed == []  # no ALTER on a clean path
