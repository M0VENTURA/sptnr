"""Tests for playlist recommendation genre track selection."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from services.playlists.recommendation_service import PlaylistRecommender


class _Row:
    def __init__(self, data: dict):
        self._data = data

    @property
    def _mapping(self) -> dict:
        return self._data


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        return _FakeResult(self._rows)

    def commit(self):
        pass

    def rollback(self):
        pass


def _genre_rows() -> list[dict]:
    """Same 5 songs on a studio album, a live release and a compilation."""
    rows = []
    for i in range(1, 6):
        rows.append({"id": f"id{i}-studio", "artist": "Alterium", "album_artist": "Alterium", "title": f"Song {i}"})
        rows.append({"id": f"id{i}-live", "artist": "Alterium", "album_artist": "Alterium", "title": f"Song {i} (Live)"})
        rows.append({"id": f"id{i}-comp", "artist": "Alterium", "album_artist": "Various Artists", "title": f"Song {i} [Remaster]"})
    return rows


@pytest.fixture
def recommender():
    @contextmanager
    def _db():
        yield _FakeSession([_Row(r) for r in _genre_rows()])

    return PlaylistRecommender(db_connection=_db)


def test_genre_tracks_dedup_by_artist_and_title(recommender):
    track_ids = recommender._get_track_ids_for_genre("Rock")
    assert len(track_ids) == 5
    # One version per song survives (best available), no live/comp duplicates.
    assert len(set(track_ids)) == 5
    for i in range(1, 6):
        assert any(str(i) in tid for tid in track_ids)


def test_genre_tracks_empty_without_db():
    recommender = PlaylistRecommender(db_connection=None)
    assert recommender._get_track_ids_for_genre("Rock") == []
