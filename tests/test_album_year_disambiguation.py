"""Regression tests: same-name albums released in different years must not
be merged on the album page.

The album page (``/album/<artist>/<album>``) previously fetched ALL tracks by
(artist, album) — two albums sharing a name from different years (a re-release
or a genuine name collision) were merged into one page.  The fix:

1. When the URL has no year segment and the (artist, album) matches tracks
   from MULTIPLE distinct years, the page defaults to the MOST RECENT year.
2. A year selector links to ``/album/<artist>/<album>/<year>`` editions.
"""

from __future__ import annotations

from sqlalchemy import text


def _seed_album(db_session, track_id, artist, album, title, year, release_year=None):
    db_session.execute(
        text("""
            INSERT INTO tracks (id, artist, album, title, file_path, year, release_year)
            VALUES (:id, :artist, :album, :title, :file_path, :year, :release_year)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": track_id,
            "artist": artist,
            "album": album,
            "title": title,
            "file_path": f"/music/{artist}/{album}/{title}.flac",
            "year": year,
            "release_year": release_year,
        },
    )
    db_session.commit()


class TestAlbumYearDisambiguation:
    async def test_route_defaults_to_most_recent_year(self, app, client, db_session):
        """Opening /album/<artist>/<album> with no year must show only the
        most recent edition's tracks when the name spans multiple years."""
        _seed_album(db_session, "t1", "Artist", "Same Name", "Track One", "1999")
        _seed_album(db_session, "t2", "Artist", "Same Name", "Track Two", "1999")
        _seed_album(db_session, "t3", "Artist", "Same Name", "Remaster Track", "2015")

        resp = await client.get("/album/Artist/Same%20Name")
        assert resp.status_code == 200
        body = await resp.get_data(as_text=True)
        # The 2015 edition's track appears…
        assert "Remaster Track" in body
        # …and the 1999-only tracks do NOT (they belong to the other edition).
        assert "Track One" not in body
        assert "Track Two" not in body
        # The year selector surfaces both editions.
        assert "2015" in body
        assert "1999" in body

    async def test_route_year_segment_shows_that_edition(self, app, client, db_session):
        """Opening /album/<artist>/<album>/<year> shows exactly that year."""
        _seed_album(db_session, "t1", "Artist", "Same Name", "Track One", "1999")
        _seed_album(db_session, "t3", "Artist", "Same Name", "Remaster Track", "2015")

        resp = await client.get("/album/Artist/Same%20Name/1999")
        assert resp.status_code == 200
        body = await resp.get_data(as_text=True)
        assert "Track One" in body
        assert "Remaster Track" not in body

    async def test_single_year_album_unaffected(self, app, client, db_session):
        """An album with a single year is shown in full (no year selector)."""
        _seed_album(db_session, "t1", "Artist", "Only Album", "Track One", "2001")
        _seed_album(db_session, "t2", "Artist", "Only Album", "Track Two", "2001")

        resp = await client.get("/album/Artist/Only%20Album")
        assert resp.status_code == 200
        body = await resp.get_data(as_text=True)
        assert "Track One" in body
        assert "Track Two" in body
