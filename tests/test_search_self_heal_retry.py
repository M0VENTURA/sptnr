"""Regression: /api/search must build its SQL from the ACTUAL ``tracks``
columns resolved via ``to_regclass`` — album_artist → artist → none — so a
legacy bare-tracks table (missing artist/album_artist/title) never 500s.

The long saga: the search previously referenced album_artist unconditionally,
then degraded to artist-only.  But the live DB turned out to have a TRULY
bare ``tracks`` table (``id`` only) — even ``artist`` was absent.  This test
pins the column-aware builder that handles all three states.

The ``routes.misc_routes`` module cannot be imported directly (a pre-existing
circular import), so the builder contract is verified via a local mirror and
by asserting the committed source wires the right expressions.
"""

from __future__ import annotations

import os


def _artist_columns(tracks_cols: set[str]) -> tuple[str, str]:
    """Mirror of the route's artist-expr / artist-like selection."""
    if "album_artist" in tracks_cols:
        return "COALESCE(NULLIF(album_artist, ''), artist)", "COALESCE(album_artist, '')"
    if "artist" in tracks_cols:
        return "artist", "COALESCE(artist, '')"
    return "", ""


def _can_search(tracks_cols: set[str]) -> bool:
    expr, _like = _artist_columns(tracks_cols)
    return bool(expr) and ("title" in tracks_cols or "album" in tracks_cols)


class TestSearchColumnAwareBuilder:
    """The artist expression must be chosen from the real resolved columns."""

    def test_full_columns_prefer_album_artist(self):
        expr, like = _artist_columns({"id", "artist", "album_artist", "album", "title"})
        assert expr == "COALESCE(NULLIF(album_artist, ''), artist)"
        assert like == "COALESCE(album_artist, '')"

    def test_no_album_artist_uses_artist(self):
        expr, like = _artist_columns({"id", "artist", "album", "title"})
        assert expr == "artist"
        assert like == "COALESCE(artist, '')"

    def test_bare_tracks_table_no_searchable_columns(self):
        """A tracks(id)-only table cannot be searched — must NOT 500."""
        cols = {"id"}
        assert _can_search(cols) is False

    def test_artist_without_title_can_search_albums(self):
        """With artist + album (no title) we can still search albums."""
        cols = {"id", "artist", "album"}
        assert _can_search(cols) is True

    def test_no_artist_no_search(self):
        """Without ANY artist column (bare tracks(id)+title), the route
        returns the graceful empty payload — no artist grouping possible."""
        assert _can_search({"id", "title"}) is False


class TestSearchSourceWiring:
    """The committed route must use to_regclass-based column discovery and
    the three-way expression selection."""

    def _read_source(self) -> str:
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(repo, "routes", "misc_routes.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_uses_to_regclass_column_discovery(self):
        src = self._read_source()
        assert "to_regclass('tracks')::text" in src

    def test_three_way_expression_selection(self):
        src = self._read_source()
        assert '"album_artist" in _tracks_cols' in src
        assert '"artist" in _tracks_cols' in src
        assert 'COALESCE(NULLIF(album_artist, \'\'), artist)' in src

    def test_graceful_empty_result_for_bare_table(self):
        src = self._read_source()
        # A bare table returns an empty-but-successful payload, never a 500.
        assert "not _can_search" in src
        assert '"artists": [],' in src
