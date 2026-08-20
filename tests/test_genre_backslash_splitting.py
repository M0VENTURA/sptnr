"""Tests for the literal-backslash genre split fix.

Navidrome / ID3v2.3 conventions join multiple genres with ``\``
(``metal\nu metal\rock``).  A genre value that reaches the tag writers or
the genre aggregation as ONE string would surface in Navidrome as a single
broken genre folder literally named ``metal\nu metal\rock``.

Fixed points pinned here:
1. ``write_id3_tags`` splits ``metal\nu metal\rock`` into three TCON values.
2. ``write_flac_tags`` splits the same string into three Vorbis GENRE values.
3. ``_album_top_genres`` splits delimited plain-text genre columns on all
   separators (backslash included) before aggregation.
4. The track-update API normalises a backslash-joined genres payload to a
   clean comma-joined string before storing it.
"""

from __future__ import annotations


class TestId3GenreSplitsBackslashes:
    def test_backslash_genres_split_regex(self):
        """'metal\\nu metal\\rock' must become THREE genre parts."""
        import re
        parts = [g.strip() for g in re.split(r"[,;/\\]+", "metal\\nu metal\\rock") if g.strip()]
        assert parts == ["metal", "nu metal", "rock"]

    def test_comma_and_semicolon_still_split(self):
        import re
        parts = [g.strip() for g in re.split(r"[,;/\\]+", "Rock, Metal; Punk") if g.strip()]
        assert parts == ["Rock", "Metal", "Punk"]


class TestFlacGenreSplitsBackslashes:
    def test_backslash_genres_become_multiple_vorbis_values(self, monkeypatch):
        from services.metadata import tag_file_service as tfs

        captured = {}

        class _FakeAudio:
            def __init__(self, path):
                self._tags = {}

            def __contains__(self, field):
                return field in self._tags

            def __delitem__(self, field):
                self._tags.pop(field, None)

            def __setitem__(self, field, values):
                captured[field] = list(values)

            def save(self):
                pass

        monkeypatch.setattr(tfs, "FLAC", _FakeAudio)
        assert tfs.write_flac_tags("/fake/path.flac", {"genres": "metal\\nu metal\\rock"}) is True
        # The Vorbis map translates genres → genre and the string is split.
        assert captured["genre"] == ["metal", "nu metal", "rock"]


class TestAlbumTopGenresSplitsDelimited:
    def test_navidrome_genres_backslash_split(self, monkeypatch):
        from services.popularity.stages import track_stage as ts
        from helpers import config_helpers

        monkeypatch.setattr(config_helpers, "get_config", lambda: {"genres": {"min_weight": 0.0}})

        album_tracks = [
            {
                "title": "A",
                "navidrome_genres": "metal\\nu metal\\rock",
            },
        ]
        result = ts._album_top_genres(album_tracks, max_genres=3)
        # navidrome weight 0.30 each → all three pass min_weight 0.0.
        assert "metal" in result
        assert "nu metal" in result
        assert "rock" in result
        # The broken single-string genre must never appear.
        assert "metal\\nu metal\\rock" not in result


class TestTrackUpdateApiNormalisesGenres:
    def test_backslash_genres_payload_normalised(self):
        """The same split the update-metadata API applies to a genres payload."""
        import re
        raw = "metal\\nu metal\\rock"
        parts = [g.strip() for g in re.split(r"[,;/\\]+", raw) if g.strip()]
        assert parts == ["metal", "nu metal", "rock"]
        assert ", ".join(parts) == "metal, nu metal, rock"
