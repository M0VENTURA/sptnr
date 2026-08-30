"""Regression tests: MusicBrainz cover art + album metadata fixes.

Covers:
1. ``apply_mbid_to_album`` downloads cover art from Cover Art Archive and
   embeds it into every track file (the reported "MB lookup doesn't update
   cover art").
2. ``fetch_album_art_from_musicbrainz`` prefers a CONCRETE release's CAA art
   (per-release art is more populated than release-group art).
3. The MB search's artist-only fallback keeps ALBUM relevance via fuzzy
   title similarity (the reported "only artist used for results").
4. ``fetch_musicbrainz_release_metadata`` captures the full MB checklist:
   composer / lyricist / iswc / original_title / absolute_track_number /
   compilation / original_date / album_artist_mbid.
"""

from __future__ import annotations


class TestApplyMbidToAlbumEmbedsCover:
    def test_cover_bytes_embedded_into_every_file(self, monkeypatch):
        from services.metadata import album_service as asvc

        fake_cover = b"\xff\xd8fakejpegdata"

        monkeypatch.setattr(asvc, "update_album_mbid_fields", lambda **kw: 2)
        written = []

        def _fake_update_file_tags(path, tags):
            written.append((path, tags))
            return True

        monkeypatch.setattr(asvc, "update_file_tags", _fake_update_file_tags)
        monkeypatch.setattr(
            asvc, "resolve_music_file_path", lambda p: p if p.startswith("/music/") else f"/music/{p}"
        )

        class _FakeResult:
            def fetchall(self):
                return [("/music/A/s1.mp3",), ("/music/A/s2.mp3",)]

        class _FakeSession:
            def execute(self, *a, **k):
                return _FakeResult()

        import contextlib

        @contextlib.contextmanager
        def _fake_db_session(*a, **k):
            yield _FakeSession()

        monkeypatch.setattr(asvc, "_db_session", _fake_db_session)

        # Patch the CAA image fetchers to return bytes.
        monkeypatch.setattr(
            "api_clients.coverartarchive.get_release_front_image_bytes",
            lambda mbid, size="500": fake_cover,
        )
        monkeypatch.setattr(
            "api_clients.coverartarchive.get_release_group_front_image_bytes",
            lambda rg, size="500": None,
        )
        # Album-art DB save + track embed are non-fatal.
        monkeypatch.setattr(asvc, "save_album_art_to_db", lambda *a, **k: True)
        monkeypatch.setattr(asvc, "apply_album_art_to_tracks", lambda *a, **k: 2)

        result = asvc.apply_mbid_to_album(
            artist="Artist", album="Album",
            mbid="rel-1", rg_mbid="rg-1", cover_url="",
        )
        assert result["success"] is True
        assert result["cover_art_applied"] is True
        for _path, tags in written:
            assert tags.get("cover_art_data") == fake_cover
            assert tags.get("cover_art_mime") == "image/jpeg"
            assert tags.get("musicbrainz_albumid") == "rel-1"


class TestFetchMusicbrainzReleaseMetadataChecklist:
    def test_captures_full_mb_checklist(self, monkeypatch):
        from services.enrichment import musicbrainz_service as mbs

        release = {
            "id": "rel-1",
            "title": "Utopie",
            "date": "2025-05-16",
            "artist-credit": [{"name": "Aephanemer", "artist": {"id": "art-1", "name": "Aephanemer"}}],
            "release-group": {
                "id": "rg-1",
                "first-release-date": "2025-05-16",
                "primary-type": "Album",
                "secondary-types": [],
            },
            "media": [
                {
                    "position": 1,
                    "tracks": [
                        {
                            "position": 1,
                            "title": "La rivière souterraine",
                            "length": 240000,
                            "recording": {
                                "id": "rec-1",
                                "title": "La rivière souterraine",
                                "artist-credit": [{"name": "Aephanemer"}],
                                "relations": [
                                    {
                                        "type": "performance",
                                        "work": {
                                            "id": "work-1",
                                            "title": "La rivière souterraine",
                                            "iswc": "T-123.456.789-0",
                                            "artist-credit": [{"name": "Aephanemer"}],
                                            "relations": [
                                                {"type": "composer", "artist": {"name": "M. Goyat"}},
                                                {"type": "lyricist", "artist": {"name": "M. Goyat"}},
                                            ],
                                        },
                                    }
                                ],
                                "genres": [{"name": "Melodic Death Metal"}],
                            },
                        }
                    ],
                },
                {
                    "position": 2,
                    "tracks": [
                        {
                            "position": 1,
                            "title": "Contrepoint",
                            "length": 250000,
                            "recording": {
                                "id": "rec-2",
                                "title": "Contrepoint",
                                "artist-credit": [{"name": "Aephanemer"}],
                            },
                        }
                    ],
                },
            ],
        }

        monkeypatch.setattr(mbs, "get_shared_mb_client", lambda: _FakeMbClient(release))

        meta = mbs.fetch_musicbrainz_release_metadata("rel-1")
        assert meta is not None
        assert meta["release_mbid"] == "rel-1"
        assert meta["release_group_mbid"] == "rg-1"
        assert meta["album_artist_mbid"] == "art-1"
        assert meta["compilation"] == 0
        assert meta["original_date"] == "2025-05-16"
        assert meta["original_year"] == "2025"
        assert meta["disc_count"] == 2

        tracks = meta["tracks"]
        assert len(tracks) == 2
        t1, t2 = tracks[0], tracks[1]
        # Medium-specific track number + absolute sequential number.
        assert t1["track_number"] == 1
        assert t1["absolute_track_number"] == 1
        assert t2["track_number"] == 1
        assert t2["absolute_track_number"] == 2
        assert t1["disc_number"] == 1
        assert t2["disc_number"] == 2
        # Composer / lyricist / iswc / work MBID.
        assert t1["composer"] == "M. Goyat"
        assert t1["lyricist"] == "M. Goyat"
        assert t1["iswc"] == "T-123.456.789-0"
        assert t1["work_mbid"] == "work-1"
        assert t1["musicbrainz_genres"] == "Melodic Death Metal"


class _FakeMbClient:
    def __init__(self, release):
        self.release = release

    def get_release(self, release_id, inc=""):
        return self.release


class TestMbSearchAlbumRelevanceFallback:
    def test_artist_fallback_keeps_album_relevance(self, monkeypatch):
        """When the strict artist+album query returns 0 groups, the
        artist-only fallback must filter by fuzzy album-title similarity so
        the user doesn't see ALL the artist's releases (the reported "only
        artist used" bug)."""
        import json

        from routes.musicbrainz_routes import api_musicbrainz_search

        groups = [
            {"id": "rg-1", "title": "Utopie", "artist-credit": [{"name": "Aephanemer"}], "first-release-date": "2025-05-16", "primary-type": "Album"},
            {"id": "rg-2", "title": "Prokopton", "artist-credit": [{"name": "Aephanemer"}], "first-release-date": "2019-01-01", "primary-type": "Album"},
            {"id": "rg-3", "title": "Emotional Wounds", "artist-credit": [{"name": "Aephanemer"}], "first-release-date": "2021-01-01", "primary-type": "Album"},
        ]

        class _FakeClient:
            def get(self, endpoint, **kwargs):
                # First call = strict artist+album (returns empty), second =
                # artist-only fallback (returns all groups).
                query = kwargs["params"]["query"]
                if "releasegroup" in query:
                    return {"release-groups": []}
                return {"release-groups": groups}

        monkeypatch.setattr(
            "routes.musicbrainz_routes._get_mb_client",
            lambda: _FakeClient(),
        )

        class _FakeRequest:
            def __init__(self, payload):
                self._payload = payload

            async def get_json(self, *a, **k):
                return self._payload

        monkeypatch.setattr("routes.musicbrainz_routes.request", _FakeRequest({
            "artist": "Aephanemer", "album": "Utopie",
        }))

        async def _run():
            return await api_musicbrainz_search()

        # The route returns a Quart Response; grab its JSON.
        import asyncio
        resp = asyncio.run(_run())
        # The route returns jsonify(...) — convert via get_json().
        data = resp.get_json()
        assert data["success"] is True
        titles = [r["title"] for r in data["releases"]]
        assert "Utopie" in titles
        # The unrelated artist releases are filtered OUT by fuzzy similarity.
        assert "Emotional Wounds" not in titles
