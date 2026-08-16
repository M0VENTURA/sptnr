"""Basic database tests for Popularr."""

from __future__ import annotations

import pytest
from sqlalchemy import text


class TestDatabaseConnection:
    """Verify database engine and session work."""

    def test_db_session_executes_query(self, db_session):
        """A simple SELECT 1 should return a result."""
        result = db_session.execute(text("SELECT 1 AS val"))
        row = result.fetchone()
        assert row is not None
        assert row[0] == 1

    def test_track_insert_and_query(self, db_session, sample_track):
        """Inserting a track and querying it should work."""
        result = db_session.execute(
            text("SELECT id, artist, title FROM tracks WHERE id = :id"),
            {"id": "test-track-001"},
        )
        row = result.fetchone()
        assert row is not None
        assert row[1] == "Test Artist"
        assert row[2] == "Test Track"

    def test_track_not_found(self, db_session):
        """Querying a non-existent track returns None."""
        result = db_session.execute(
            text("SELECT id FROM tracks WHERE id = :id"),
            {"id": "nonexistent"},
        )
        assert result.fetchone() is None


class TestTrackRepository:
    """Test the tracks repository layer."""

    def test_insert_or_update_track(self, db_session, sample_track):
        """Insert a track via the repository function."""
        from db.repositories.tracks import insert_or_update_track

        insert_or_update_track(
            track_id="test-insert-001",
            track_data={
                "artist_id": "artist-001",
                "album": "Test Insert Album",
                "title": "Test Insert Track",
                "genres": '["Rock","Pop"]',
                "spotify_score": 75.0,
                "lastfm_score": 60.0,
                "listenbrainz_score": 0.0,
                "age_score": 10.0,
                "final_score": 70.0,
                "stars": 4,
                "is_single": False,
                "single_confidence": 0.0,
            },
        )

        result = db_session.execute(
            text("SELECT title, final_score, stars FROM tracks WHERE id = :id"),
            {"id": "test-insert-001"},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "Test Insert Track"
        assert float(row[1]) == 70.0
        assert int(row[2]) == 4

    def test_get_tracks_by_artist(self, db_session, sample_track):
        """Test fetching tracks by artist."""
        from db.repositories.tracks import get_tracks_by_artist

        tracks = get_tracks_by_artist("artist-001")
        # Should at minimum not crash
        assert isinstance(tracks, list)


class TestPopularityRepository:
    """Test popularity repository functions."""

    def test_save_to_db(self, db_session):
        """Test the save_to_db function with minimal data."""
        from db.repositories.popularity_repository import save_to_db

        result = save_to_db({
            "id": "test-pop-001",
            "artist": "Pop Artist",
            "title": "Pop Track",
            "final_score": 85.0,
            "stars": 5,
        })
        assert result is True

        result = db_session.execute(
            text("SELECT final_score FROM tracks WHERE id = :id"),
            {"id": "test-pop-001"},
        )
        row = result.fetchone()
        assert row is not None
        assert float(row[0]) == 85.0
