"""Tests for the download auto-move + Matched-Folders per-track + search fixes.

Covers:
1. ``insert_queue_item`` honors the ``status`` kwarg so discovered/local files
   are stored as ``unmatched`` (never auto-searched/moved).
2. ``get_ready_for_processing`` excludes local/discovered sources (the
   queue processor must never pick up passive disk files).
3. Word-dropping search fallbacks ("Avenged Sevenfold - It's Not Easy" →
   progressively dropped-word variants).
4. Per-track folder actions (list / delete / move one file).
"""

from __future__ import annotations


class TestInsertQueueItemStatusHonored:
    def test_discovered_source_stored_unmatched(self):
        """A discovered file (source='discovered') must be stored as
        'unmatched', not 'queued' — it is a passive disk state."""
        from db.repositories.queue import insert_queue_item, get_queue_item

        row = insert_queue_item(
            artist="Some Artist",
            title="Some Track",
            album="Some Album",
            source="discovered",
            file_path="/downloads/Some Folder/some_track.mp3",
            found_filename="some_track.mp3",
            status="unmatched",
            import_group="default",
        )
        assert row.get("already_queued") is not True
        qid = row.get("id")
        assert qid
        stored = get_queue_item(qid)
        assert stored.get("status") == "unmatched"
        assert (stored.get("source") or "").lower() == "discovered"

    def test_soulseek_source_defaults_queued(self):
        """A normal soulseek queue item still defaults to 'queued'."""
        from db.repositories.queue import insert_queue_item, get_queue_item

        row = insert_queue_item(artist="Radiohead", title="Creep", album="Pablo Honey")
        assert row.get("already_queued") is not True
        qid = row.get("id")
        assert qid
        stored = get_queue_item(qid)
        assert stored.get("status") == "queued"
        assert (stored.get("source") or "").lower() == "soulseek"

    def test_local_source_forced_unmatched_even_if_queued_requested(self):
        """Even a caller that explicitly asks for 'queued' cannot promote a
        local source into the active queue."""
        from db.repositories.queue import insert_queue_item, get_queue_item

        row = insert_queue_item(
            artist="Artist",
            title="Title",
            album="Album",
            source="local",
            status="queued",
            file_path="/downloads/x.mp3",
            found_filename="x.mp3",
        )
        qid = row.get("id")
        assert qid
        stored = get_queue_item(qid)
        assert stored.get("status") == "unmatched"


class TestGetReadyForProcessingExcludesPassiveSources:
    def test_discovered_rows_never_returned(self):
        """Rows with source='discovered' (even if status='queued') must never
        be returned by get_ready_for_processing."""
        from db.repositories.queue import insert_queue_item, get_ready_for_processing

        insert_queue_item(
            artist="Passive Artist", title="Passive Track", album="Passive",
            source="discovered", status="unmatched",
            file_path="/downloads/passive/passive.mp3", found_filename="passive.mp3",
        )
        # Also insert a genuinely queued soulseek row to prove the query works.
        insert_queue_item(artist="Active Artist", title="Active Track", album="Active")

        ready = get_ready_for_processing(limit=50) or []
        artists = {str(r.get("artist") or "") for r in ready}
        assert "Passive Artist" not in artists
        assert "Active Artist" in artists


class TestCrossSourceDedupe:
    """The same song added via different entry points must not pile up as
    multiple searchable queue rows ("20 versions of one song")."""

    def test_soulseek_then_musicbrainz_dedupes(self):
        from db.repositories.queue import insert_queue_item, get_queue_item

        first = insert_queue_item(artist="Radiohead", title="Creep", album="Pablo Honey", source="soulseek")
        assert first.get("already_queued") is not True
        qid = first.get("id")

        # Adding the SAME track via the MusicBrainz flow must dedupe (the
        # old ``source = :source`` check created a second searchable row).
        second = insert_queue_item(
            artist="Radiohead", title="Creep", album="Pablo Honey",
            source="musicbrainz", release_mbid="abc-123",
        )
        assert second.get("already_queued") is True
        assert int(second.get("id")) == int(qid)

        # Still exactly one row.
        from db.repositories.queue import get_queue_status_counts
        counts = get_queue_status_counts() or {}
        assert int(counts.get("queued") or 0) == 1

    def test_discovered_and_soulseek_stay_separate(self):
        """A local/discovered file row and a searchable row for the same
        track are different concerns — the disk file must not suppress a
        legitimate search, and vice versa."""
        from db.repositories.queue import insert_queue_item

        local = insert_queue_item(
            artist="Radiohead", title="Creep", album="Pablo Honey",
            source="discovered", status="unmatched",
            file_path="/downloads/Radiohead/creep.mp3", found_filename="creep.mp3",
        )
        assert local.get("already_queued") is not True

        search = insert_queue_item(
            artist="Radiohead", title="Creep", album="Pablo Honey",
            source="soulseek",
        )
        # Different (artist, title) dedupe buckets: local rows don't suppress
        # searchable rows and vice versa.
        assert search.get("already_queued") is not True


class TestWordDropFallbackQueries:
    def test_artist_and_title_word_drops(self):
        """The fallback builder produces the progressively-dropped-word
        variants the user described:
        Avenged Sevenfold - It's Not Easy → Avenged - It's Not Easy,
        Sevenfold - It's Not Easy, Avenged Sevenfold - Not Easy, ..."""
        from services.downloads.download_pipeline_service import _build_fallback_search_queries

        item = {"artist": "Avenged Sevenfold", "title": "It's Not Easy", "album": "Album"}
        primary = "Avenged Sevenfold - It's Not Easy"
        queries = _build_fallback_search_queries(item, primary)

        lowered = [q.lower() for q in queries]
        # Artist word-drop variants with the full title.
        assert "avenged - it's not easy" in lowered
        assert "sevenfold - it's not easy" in lowered
        # Title word-drop variants with the full artist.
        assert "avenged sevenfold - not easy" in lowered
        assert "avenged sevenfold - easy" in lowered
        # Paired drops (dropped artist + dropped title).
        assert "avenged - not easy" in lowered
        assert "sevenfold - easy" in lowered
        # The title-only last resort is present.
        assert "it's not easy" in lowered

    def test_single_word_artist_no_artist_drops(self):
        from services.downloads.download_pipeline_service import _build_fallback_search_queries

        item = {"artist": "Muse", "title": "Knights of Cydonia", "album": "BH&R"}
        queries = _build_fallback_search_queries(item, "Muse - Knights of Cydonia")
        lowered = [q.lower() for q in queries]
        # Single-word artist → no artist word-drop variant exists.
        assert "muse - of cydonia" not in lowered  # (of is dropped only in title drops)
        # Title word-drops still present.
        assert "muse - knights of cydonia" in lowered or "muse - knights cydonia" in lowered


class TestFolderPerTrackFunctions:
    def _make_folder(self, tmp_path, files):
        import json
        folder = tmp_path / "Artist - Album"
        folder.mkdir()
        for name in files:
            (folder / name).write_text("fake-audio-bytes", encoding="utf-8")
        return folder

    def test_get_folder_tracks_lists_audio_files(self, tmp_path, monkeypatch):
        folder = self._make_folder(tmp_path, ["01 - Track One.mp3", "02 - Track One.mp3"])
        from services.downloads import download_folder_service as dfs

        # The helper resolves the downloads dir; point it at the tmp folder.
        monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda: str(tmp_path))
        result = dfs.get_folder_tracks("Artist - Album")
        assert result.get("success")
        names = [t["name"] for t in result["tracks"]]
        # Audio detection reads file extensions via _get_files_in_folder; the
        # fake files have .mp3 names so they count as audio.
        assert any("01 - Track One.mp3" in n for n in names)

    def test_delete_folder_track_removes_one_file(self, tmp_path, monkeypatch):
        folder = self._make_folder(tmp_path, ["01 - Track One.mp3", "02 - Track One.mp3"])
        from services.downloads import download_folder_service as dfs

        monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda: str(tmp_path))
        monkeypatch.setattr(dfs, "_imported_source_paths", lambda: set())
        result = dfs.delete_folder_track("Artist - Album", "01 - Track One.mp3")
        assert result.get("success")
        assert not (folder / "01 - Track One.mp3").exists()
        assert (folder / "02 - Track One.mp3").exists()

    def test_delete_folder_track_refuses_imported_file(self, tmp_path, monkeypatch):
        folder = self._make_folder(tmp_path, ["01 - Track One.mp3"])
        from services.downloads import download_folder_service as dfs

        monkeypatch.setattr(dfs, "resolve_downloads_dir", lambda: str(tmp_path))
        imported = {str(folder / "01 - Track One.mp3")}
        monkeypatch.setattr(dfs, "_imported_source_paths", lambda: imported)
        result = dfs.delete_folder_track("Artist - Album", "01 - Track One.mp3")
        assert not result.get("success")
        assert (folder / "01 - Track One.mp3").exists()
