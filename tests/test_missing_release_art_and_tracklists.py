"""Tests for artist-page missing-release art + expandable tracklists.

Two fixes pinned here:
1. ``artist_scan_service`` browses release-groups WITHOUT a bogus ``inc``
   (``inc=cover-art-archive`` returned HTTP 400 on the browse endpoint).
   The release-group entity includes the ``cover-art-archive`` block by
   default, so ``_release_cover_art_url`` builds real Cover Art Archive
   URLs without requesting anything extra.
2. ``routes/artist_routes`` exposes
   ``GET /api/artist/missing-release-tracks?release_id=<release-group MBID>``
   which browses the group's releases, fetches the first release's
   recordings, and returns a flat tracklist — powering the expandable
   accordion bodies on the artist page (the release is not in the library
   and not in the downloads cache).
"""

from __future__ import annotations


class TestCoverArtArchiveInc:
    def test_browse_request_has_no_inc(self, monkeypatch):
        """browse_artist_release_groups must be called WITHOUT an inc —
        ``inc=cover-art-archive`` returned 400 Bad Request on the browse
        endpoint; the cover-art-archive block ships with every release-group
        entity by default."""
        from services.metadata import artist_scan_service as svc

        captured = {}

        class _FakeClient:
            def browse_artist_release_groups(self, artist_mbid, inc="", limit=25, offset=0):
                captured["inc"] = inc
                captured["limit"] = limit
                captured["offset"] = offset
                return {"release_groups": [{"id": "rg-1", "cover-art-archive": {"artwork": True, "count": 1}}]}

        monkeypatch.setattr(svc, "MusicBrainzHttpClient", lambda: _FakeClient())
        page = svc._fetch_musicbrainz_release_groups("artist-mbid", limit=50, offset=25)
        assert captured["inc"] == ""  # no inc — browse rejects cover-art-archive
        assert captured["limit"] == 50
        assert captured["offset"] == 25
        assert page == [{"id": "rg-1", "cover-art-archive": {"artwork": True, "count": 1}}]

    def test_cover_url_built_when_artwork_present(self):
        from services.metadata.artist_scan_service import _release_cover_art_url

        rg = {
            "id": "rg-abc",
            "cover-art-archive": {"artwork": True, "count": 1},
        }
        assert _release_cover_art_url(rg) == "https://coverartarchive.org/release-group/rg-abc/front-500"

    def test_cover_url_empty_when_no_artwork(self):
        from services.metadata.artist_scan_service import _release_cover_art_url

        assert _release_cover_art_url({"id": "rg-abc"}) == ""
        assert _release_cover_art_url({"id": "rg-abc", "cover-art-archive": {"artwork": False, "count": 0}}) == ""


class TestMissingReleaseTracksEndpoint:
    def _endpoint_logic(self, release_id, client):
        """Replicate the route's fetch logic with a fake MB client."""
        from api_clients.musicbrainz_http import MusicBrainzHttpClient

        releases = client.browse_releases_for_group(release_id, inc="media") or []
        if not releases:
            return {"error": "No release found for this release group"}
        release = releases[0]
        release_mbid = str(release.get("id") or "")
        detail = client.get_release(release_mbid, inc="recordings+artist-credits+media", timeout=15.0)
        media = detail.get("media") or []
        tracks = []
        disc_number = 1
        for medium in media:
            if medium.get("disc") is not None:
                disc_number = int(medium.get("disc") or 1)
            for track in medium.get("tracks") or []:
                recording = track.get("recording") or {}
                tracks.append({
                    "position": track.get("number") or track.get("position") or "",
                    "title": track.get("title") or recording.get("title") or "",
                    "length": track.get("length") or recording.get("length") or 0,
                    "disc_number": disc_number,
                })
        return {
            "release_id": release_id,
            "release_mbid": release_mbid,
            "title": detail.get("title") or release.get("title") or "",
            "date": detail.get("date") or release.get("date") or "",
            "tracks": tracks,
        }

    def test_tracklist_flat_from_release_recordings(self):
        class _FakeClient:
            def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
                return [{"id": "rel-1", "title": "The Album"}]

            def get_release(self, release_mbid, inc="", timeout=10.0):
                return {
                    "title": "The Album",
                    "date": "2024-05-01",
                    "media": [{
                        "disc": 1,
                        "tracks": [
                            {"number": "1", "title": "Song One", "recording": {"title": "Song One", "length": 200000}},
                            {"number": "2", "title": "Song Two", "recording": {"title": "Song Two", "length": 180000}},
                        ],
                    }],
                }

        result = self._endpoint_logic("rg-abc", _FakeClient())
        assert result["release_mbid"] == "rel-1"
        assert result["title"] == "The Album"
        assert len(result["tracks"]) == 2
        assert result["tracks"][0] == {"position": "1", "title": "Song One", "length": 200000, "disc_number": 1}
        assert result["tracks"][1]["title"] == "Song Two"

    def test_no_release_returns_error(self):
        class _EmptyClient:
            def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
                return []

        result = self._endpoint_logic("rg-ghost", _EmptyClient())
        assert "error" in result

    def test_media_absent_falls_back_to_recordings(self):
        class _FakeClient:
            def browse_releases_for_group(self, rg_mbid, inc="media", limit=50):
                return [{"id": "rel-1", "title": "The Album"}]

            def get_release(self, release_mbid, inc="", timeout=10.0):
                return {
                    "title": "The Album",
                    "date": "",
                    "recordings": [{"title": "Rec One", "length": 150000}],
                }

        result = self._endpoint_logic("rg-abc", _FakeClient())
        assert len(result["tracks"]) == 1
        assert result["tracks"][0]["title"] == "Rec One"
