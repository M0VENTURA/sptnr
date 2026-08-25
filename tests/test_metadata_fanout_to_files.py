"""Tests for the metadata fan-out fixes (album/track edits → audio files).

Three behaviours are pinned:

1. ``build_tag_updates`` — the shared DB-column → tag-writer mapper maps
   album-edit payload fields (title/artist/year/MBIDs/release info) to the
   exact frames ``write_tags_to_file`` understands, so the album page writes
   the same tags the track page does (previously album edits were DB-only
   except genres).
2. ``write_flac_tags`` — a genres LIST becomes multiple Vorbis ``GENRE``
   values, never the literal ``"['Rock', 'Metal']"`` string, and the MBID /
   year field names map to their standard Vorbis names (``MUSICBRAINZ_*`` /
   ``date``).
3. ``resolve_music_file_path`` is the single shared resolver used by the
   album service paths (``apply_genres_to_album``, ``bulk_tag_tracks``,
   ``update_album_ids``) so file writes never fail on a stored path.
4. ``_write_tags_atomic`` — tag writes go to a temp sibling + ``os.replace``
   so a reader/streamer never sees a torn write, and a failed write leaves
   the original file byte-for-byte untouched.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager

import pytest


@contextmanager
def _session_cm(session):
    yield session


class TestAtomicTagWrites:
    """Tag writes must be atomic: temp sibling + os.replace, original
    preserved on failure."""

    def test_success_replaces_original_and_cleans_temp(self, tmp_path, monkeypatch):
        """A successful write lands on the original path and leaves no temp
        sibling behind."""
        from services.metadata import tag_file_service as tfs
        import helpers.config_helpers as ch

        mp3 = tmp_path / "track.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 500)
        monkeypatch.setattr(ch, "get_tagging_config", lambda: {"write_tags_to_file": True})

        written = {}

        class _FakeAudio:
            def __init__(self, path):
                self._path = str(path)
                self._tags = {}

            def __contains__(self, field):
                return field in self._tags

            def __setitem__(self, field, values):
                self._tags[field] = list(values)

            def __delitem__(self, field):
                self._tags.pop(field, None)

            def delall(self, frame_id):
                self._tags.pop(frame_id, None)

            def add(self, frame):
                pass

            def add_tags(self):
                pass

            def save(self, *args, **kw):
                written["path"] = self._path

        monkeypatch.setattr(tfs, "MP3", _FakeAudio)
        monkeypatch.setattr(tfs, "ID3", _FakeAudio)
        ok = tfs.update_file_tags(str(mp3), {"title": "Atomic"})
        assert ok is True
        # The writer mutated a TEMP sibling, not the original path.
        assert written.get("path", "").endswith(".mp3")
        assert written["path"] != str(mp3)
        # os.replace moved the temp over the original.
        assert mp3.exists()
        # No .populartag_* temp files remain.
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".populartag_")]
        assert leftovers == []

    def test_failed_write_preserves_original(self, tmp_path, monkeypatch):
        """If the tag writer fails, the original file is untouched."""
        from services.metadata import tag_file_service as tfs

        mp3 = tmp_path / "track.mp3"
        orig_bytes = b"\xff\xfb\x90\x00" + b"\x00" * 500
        mp3.write_bytes(orig_bytes)

        def _boom(*a, **kw):
            return False

        monkeypatch.setattr(tfs, "write_id3_tags", _boom)
        ok = tfs.update_file_tags(str(mp3), {"title": "X"})
        assert ok is False
        assert mp3.read_bytes() == orig_bytes
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".populartag_")]
        assert leftovers == []

    def test_content_change_keeps_fresh_mtime(self, tmp_path, monkeypatch):
        """Navidrome compatibility: when tag bytes change, the atomic swap's
        fresh mtime is NOT restored (Navidrome keys change detection on
        path+mtime)."""
        from services.metadata import tag_file_service as tfs
        import helpers.config_helpers as ch

        mp3 = tmp_path / "track.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 500)
        # Preserve timestamps is ON (default) — but content changes.
        monkeypatch.setattr(ch, "get_tagging_config", lambda: {"write_tags_to_file": True, "preserve_file_timestamps": True})

        class _FakeAudio:
            def __init__(self, path):
                self._path = str(path)
                self._tags = {}

            def __contains__(self, field):
                return field in self._tags

            def __setitem__(self, field, values):
                self._tags[field] = list(values)

            def __delitem__(self, field):
                self._tags.pop(field, None)

            def delall(self, frame_id):
                self._tags.pop(frame_id, None)

            def add(self, frame):
                pass

            def add_tags(self):
                pass

            def save(self, *args, **kw):
                # Simulate a REAL tag write: append bytes to the temp file so
                # the content hash differs after the swap.
                with open(self._path, "ab") as fh:
                    fh.write(b"TAG")

        monkeypatch.setattr(tfs, "MP3", _FakeAudio)
        monkeypatch.setattr(tfs, "ID3", _FakeAudio)

        # Pin mtime to a clearly old value so the post-write mtime is
        # unambiguous (avoids same-nanosecond races with the file creation).
        import time as _time
        old_mtime = 1_600_000_000
        os.utime(mp3, (old_mtime, old_mtime))
        ok = tfs.update_file_tags(str(mp3), {"title": "Atomic"})
        assert ok is True
        after = os.stat(mp3).st_mtime_ns
        # The swap produced a NEW inode + fresh mtime that we keep — NOT the
        # restored old timestamp (which would hide the change from
        # Navidrome's path+mtime change detection).
        assert after // 1_000_000_000 > old_mtime

    def test_noop_write_restores_timestamps(self, tmp_path, monkeypatch):
        """When the write is a true no-op (bytes identical), timestamps are
        restored so the file is untouched."""
        from services.metadata import tag_file_service as tfs
        import helpers.config_helpers as ch

        mp3 = tmp_path / "track.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 500)
        # Pin mtime to a known old value.
        import time
        old_mtime = 1_600_000_000
        os.utime(mp3, (old_mtime, old_mtime))
        monkeypatch.setattr(ch, "get_tagging_config", lambda: {"write_tags_to_file": True, "preserve_file_timestamps": True})

        class _NoopAudio:
            def __init__(self, path):
                self._path = str(path)

            def __contains__(self, field):
                return False

            def __setitem__(self, field, values):
                pass

            def __delitem__(self, field):
                pass

            def delall(self, frame_id):
                pass

            def add(self, frame):
                pass

            def add_tags(self):
                pass

            def save(self, *args, **kw):
                # No-op writer: does NOT change the temp file bytes.
                pass

        monkeypatch.setattr(tfs, "MP3", _NoopAudio)
        monkeypatch.setattr(tfs, "ID3", _NoopAudio)
        ok = tfs.update_file_tags(str(mp3), {"title": "Noop"})
        assert ok is True
        # Timestamp restored to the pre-write value.
        assert os.stat(mp3).st_mtime == old_mtime

    def test_unsupported_extension_returns_false(self, tmp_path, monkeypatch):
        from services.metadata import tag_file_service as tfs
        import helpers.config_helpers as ch

        f = tmp_path / "track.wav"
        f.write_bytes(b"data")
        # Bypass the tagging-config gate so the format check is what's tested.
        monkeypatch.setattr(ch, "get_tagging_config", lambda: {"write_tags_to_file": True})
        ok = tfs.update_file_tags(str(f), {"title": "X"})
        assert ok is False


# ---------------------------------------------------------------------------
# build_tag_updates — shared column→tag mapper
# ---------------------------------------------------------------------------

class TestBuildTagUpdates:
    def _map(self):
        from services.metadata.tag_file_service import build_tag_updates
        return build_tag_updates

    def test_album_payload_fields_map(self):
        tags = self._map()({
            "album": "The New Album",
            "album_artist": "Band Name",
            "artist": "Band Name",
            "year": "2004",
            "musicbrainz_albumid": "release-mbid",
            "musicbrainz_releasegroupid": "rg-mbid",
            "musicbrainz_artistid": "artist-mbid",
            "recordlabel": "Label X",
        })
        assert tags["album"] == "The New Album"
        assert tags["album_artist"] == "Band Name"
        assert tags["artist"] == "Band Name"
        assert tags["year"] == "2004"
        assert tags["musicbrainz_albumid"] == "release-mbid"
        assert tags["musicbrainz_releasegroupid"] == "rg-mbid"
        assert tags["musicbrainz_artistid"] == "artist-mbid"
        assert tags["recordlabel"] == "Label X"

    def test_mbid_aliases_collapse_to_canonical_tag(self):
        tags = self._map()({
            "musicbrainz_album_mbid": "release-mbid",
            "musicbrainz_releaseid": "release-mbid",
        })
        assert tags["musicbrainz_albumid"] == "release-mbid"

    def test_empty_values_dropped(self):
        tags = self._map()({
            "album": "Keep",
            "artist": "",
            "year": None,
            "mbid": "   ",
        })
        assert "album" in tags
        assert "artist" not in tags
        assert "year" not in tags
        assert "mbid" not in tags

    def test_genres_key_present(self):
        tags = self._map()({"genres": "Rock, Metal"})
        assert tags.get("genres") == "Rock, Metal"


# ---------------------------------------------------------------------------
# write_flac_tags — list values + Vorbis field names
# ---------------------------------------------------------------------------

class TestWriteFlacTags:
    def _write(self, tags):
        from services.metadata import tag_file_service as tfs
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            # A real FLAC file is required for mutagen; if writing fails for
            # environment reasons (no mutagen), the test still verifies the
            # call path reached the writer.
            return tfs.write_flac_tags(path, tags)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_list_value_written_as_multiple_values(self, monkeypatch):
        """A genres list must NOT become str(list) — it becomes N values."""
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
        assert tfs.write_flac_tags("/fake/path.flac", {"genres": ["Rock", "Metal"]}) is True
        assert captured["genre"] == ["Rock", "Metal"]

    def test_mbid_and_year_vorbis_names(self, monkeypatch):
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
        assert tfs.write_flac_tags("/fake/path.flac", {
            "year": "2004",
            "musicbrainz_albumid": "release-mbid",
            "musicbrainz_trackid": "track-mbid",
        }) is True
        assert captured["date"] == ["2004"]
        assert captured["MUSICBRAINZ_ALBUMID"] == ["release-mbid"]
        assert captured["MUSICBRAINZ_TRACKID"] == ["track-mbid"]


# ---------------------------------------------------------------------------
# Album service paths use the shared resolver
# ---------------------------------------------------------------------------

class TestAlbumServiceUsesResolver:
    def test_apply_genres_to_album_resolves_path(self, monkeypatch):
        """A relative stored path resolves via the shared helper, not os.path."""
        from services.metadata import album_service as svc

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        root = os.path.dirname(path)
        rel = os.path.basename(path)
        monkeypatch.setenv("MUSIC_ROOT", root)

        calls = []

        class _Row:
            def get(self, key):
                return {"id": "t1", "title": "Song", "file_path": rel}.get(key)

        monkeypatch.setattr(svc, "fetch_album_tracks_for_tag_update", lambda **kw: [_Row()])
        monkeypatch.setattr(svc, "update_track_genres", lambda **kw: 1)
        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_tags",
            lambda fp, tags: calls.append(fp) or True,
        )
        result = svc.apply_genres_to_album("Artist", "Album", ["Rock"])
        try:
            assert result["updated"] == 1
            assert calls == [path]
        finally:
            os.unlink(path)

    def test_update_album_ids_writes_mbid_to_files(self, monkeypatch):
        from services.metadata import album_service as svc
        from sqlalchemy import text

        calls = []

        class _Row:
            def __init__(self, values):
                self._values = values

            def __getitem__(self, idx):
                return self._values[idx]

        class _Result:
            def __init__(self, rowcount, rows):
                self.rowcount = rowcount
                self._rows = rows

            def fetchall(self):
                return self._rows

        executed = []

        class _Session:
            def execute(self, sql, params=None):
                executed.append((str(sql), params))
                if "UPDATE tracks" in str(sql) and "discogs_album_id" not in str(sql):
                    return _Result(3, [])
                if "SELECT id, file_path" in str(sql):
                    return _Result(0, [_Row(("t1", "/music/Artist/Album/Song.mp3"))])
                return _Result(1, [])

            def commit(self):
                pass

            def rollback(self):
                pass

        monkeypatch.setattr(svc, "db_session", _session_cm(_Session()))
        monkeypatch.setattr(
            "services.metadata.tag_file_service.resolve_music_file_path",
            lambda p: p,
        )
        monkeypatch.setattr(
            "services.metadata.tag_file_service.update_file_tags",
            lambda fp, tags: calls.append((fp, tags)) or True,
        )

        result, code = svc.update_album_ids({
            "artist": "Artist",
            "album": "Album",
            "musicbrainz_release_id": "release-mbid",
            "musicbrainz_release_group_id": "rg-mbid",
        })
        assert code == 200
        assert result["rows_updated"] == 3
        assert calls == [("/music/Artist/Album/Song.mp3", {
            "musicbrainz_albumid": "release-mbid",
            "musicbrainz_releasegroupid": "rg-mbid",
        })]
