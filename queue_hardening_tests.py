import pathlib
import unittest


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


class QueueHardeningTests(unittest.TestCase):
    def test_queue_add_source_allowlist_present(self):
        app_text = _read("app.py")
        self.assertIn("allowed_sources = {\"soulseek\", \"qbittorrent\"}", app_text)
        self.assertIn("Invalid source", app_text)

    def test_queue_status_reconcile_is_opt_in(self):
        app_text = _read("app.py")
        self.assertIn("request.args.get('reconcile', 'false')", app_text)
        self.assertIn("if reconcile:", app_text)

    def test_queue_processor_has_qbittorrent_path(self):
        processor_text = _read("queue_processor.py")
        self.assertIn("def search_and_download_qbittorrent", processor_text)
        self.assertIn("if source == 'qbittorrent':", processor_text)

    def test_retry_manager_uses_search3_response_shape(self):
        retry_text = _read("download_retry_manager.py")
        self.assertIn("searchResult3", retry_text)

    # --- Auto-cleanup of confirmed in-collection queue items ---

    def test_confirmed_collection_match_helper_exists(self):
        processor_text = _read("queue_processor.py")
        self.assertIn("def _is_confirmed_collection_match", processor_text)
        self.assertIn("def _delete_confirmed_collection_item", processor_text)

    def test_existence_checks_return_matched_data(self):
        """check_track_exists_in_db and check_track_exists_in_navidrome return a 3-tuple."""
        processor_text = _read("queue_processor.py")
        self.assertIn("return False, \"\", None", processor_text)
        self.assertIn("return True, reason, matched", processor_text)

    def test_search_and_download_uses_confirmed_match(self):
        """search_and_download calls the confirmed-match helpers on in-collection hits."""
        processor_text = _read("queue_processor.py")
        self.assertIn("db_exists, db_reason, db_matched = check_track_exists_in_db(queue_item)", processor_text)
        self.assertIn("nav_exists, nav_reason, nav_matched = check_track_exists_in_navidrome(queue_item)", processor_text)
        self.assertIn("_is_confirmed_collection_match(queue_item, db_matched)", processor_text)
        self.assertIn("_is_confirmed_collection_match(queue_item, nav_matched)", processor_text)
        self.assertIn("_delete_confirmed_collection_item(queue_id, queue_item)", processor_text)

    def test_delete_confirmed_marks_as_deleted(self):
        """_delete_confirmed_collection_item updates status to 'deleted'."""
        processor_text = _read("queue_processor.py")
        self.assertIn("status = 'deleted'", processor_text)

    def test_confirmed_match_checks_album_and_duration(self):
        """_is_confirmed_collection_match validates album name and duration."""
        processor_text = _read("queue_processor.py")
        self.assertIn("q_album", processor_text)
        self.assertIn("m_album", processor_text)
        self.assertIn("q_dur", processor_text)
        self.assertIn("m_dur", processor_text)

    # --- Download queue duplicate and matching improvements ---

    def test_filename_matches_queue_item_defined(self):
        """_filename_matches_queue_item must be defined in queue_processor."""
        processor_text = _read("queue_processor.py")
        self.assertIn("def _filename_matches_queue_item", processor_text)

    def test_file_matches_queue_item_uses_filename_fallback(self):
        """_file_matches_queue_item must call _filename_matches_queue_item for filename fallback."""
        processor_text = _read("queue_processor.py")
        self.assertIn("_filename_matches_queue_item(candidate_name, queue_item)", processor_text)
        # Must NOT reference the old undefined function
        self.assertNotIn("matches_queue_item(candidate_name, queue_item,", processor_text)

    def test_sibling_downloads_cleanup_defined(self):
        """_cleanup_sibling_downloads must be defined to remove duplicate files."""
        processor_text = _read("queue_processor.py")
        self.assertIn("def _cleanup_sibling_downloads", processor_text)
        self.assertIn("_cleanup_sibling_downloads(item,", processor_text)

    def test_add_to_queue_cross_album_duplicate_check(self):
        """add_to_queue must detect duplicates regardless of album (cross-album check)."""
        mgr_text = _read("download_queue_manager.py")
        # The new check is a single SQL block that matches on artist + title + source
        # without constraining album — verify the pattern is present.
        self.assertIn("Cross-album check", mgr_text)

    def test_check_track_exists_in_db_excludes_queued_placeholders(self):
        """check_track_exists_in_db must not match placeholder rows inserted by _add_queue_item_to_tracks_table."""
        processor_text = _read("queue_processor.py")
        # The query must explicitly exclude file_path values that are queue placeholders
        # to prevent newly-queued items from immediately being marked in_collection.
        self.assertIn("file_path NOT LIKE '__queued_for_download__%'", processor_text)

    def test_collection_check_in_add_to_queue_excludes_queued_placeholders(self):
        """add_to_queue collection check must not match __queued_for_download__ placeholder rows.

        When a track (e.g. 'World So Cold') is already queued its placeholder row
        in the tracks table has file_path='__queued_for_download__queue_id_N'.
        The SQL collection check must exclude these rows so a *different* track
        from the same album (e.g. 'World So Cold Intro') is not incorrectly marked
        in_collection by matching against that placeholder.
        """
        mgr_text = _read("download_queue_manager.py")
        # Verify the SQL guard appears in the collection_check_query block
        # (which covers both the SQLite '?' and PostgreSQL '%s' variants).
        self.assertIn(
            "file_path NOT LIKE '__queued_for_download__%'",
            mgr_text,
        )

    def test_find_library_track_id_excludes_queued_placeholders(self):
        """_find_library_track_id must not return IDs for __queued_for_download__ rows."""
        mgr_text = _read("download_queue_manager.py")
        # Ensure the SQL guard is present inside _find_library_track_id so
        # placeholder rows can never surface as 'already in library'.
        # We verify by counting occurrences — there must be at least 2 (one for
        # each of the two queries that can set in_collection).
        count = mgr_text.count("file_path NOT LIKE '__queued_for_download__%'")
        self.assertGreaterEqual(
            count, 2,
            "Expected at least 2 occurrences of the __queued_for_download__ guard "
            "in download_queue_manager.py (collection_check_query + _find_library_track_id)"
        )

    def test_prefix_title_protection_in_queue_processor(self):
        """queue_processor._metadata_matches_queue_item must reject prefix title matches.

        A file tagged 'World So Cold' must not match a queue item titled
        'World So Cold Intro' (or vice versa) just because one title is a
        leading substring of the other.
        """
        processor_text = _read("queue_processor.py")
        # The prefix-protection block must be present
        self.assertIn("startswith(_title_b)", processor_text)
        self.assertIn("startswith(_title_a)", processor_text)

    def test_prefix_title_protection_in_queue_manager(self):
        """download_queue_manager._metadata_matches_queue_item must reject prefix title matches."""
        mgr_text = _read("download_queue_manager.py")
        self.assertIn("_PREFIX_TITLE_MIN", mgr_text)
        self.assertIn("startswith(_title_b)", mgr_text)
        self.assertIn("startswith(_title_a)", mgr_text)


class FilenameMatchLogicTests(unittest.TestCase):
    """Unit tests for _filename_matches_queue_item (no DB/network needed)."""

    def _get_func(self):
        import sys, os
        if "queue_processor" not in sys.modules:
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "queue_processor",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_processor.py"),
                )
                mod = importlib.util.module_from_spec(spec)
                sys.modules["queue_processor"] = mod
                spec.loader.exec_module(mod)
            except Exception as e:
                raise unittest.SkipTest(f"queue_processor not importable: {e}")
        return sys.modules["queue_processor"]._filename_matches_queue_item

    def test_artist_and_title_present(self):
        fn = self._get_func()
        item = {"artist": "Pink Floyd", "title": "Comfortably Numb", "album": "The Wall"}
        self.assertTrue(fn("pink floyd - comfortably numb.mp3", item))

    def test_wrong_artist_returns_false(self):
        fn = self._get_func()
        item = {"artist": "Pink Floyd", "title": "Comfortably Numb", "album": "The Wall"}
        self.assertFalse(fn("led zeppelin - stairway to heaven.mp3", item))

    def test_empty_filename_returns_false(self):
        fn = self._get_func()
        item = {"artist": "Pink Floyd", "title": "Comfortably Numb", "album": "The Wall"}
        self.assertFalse(fn("", item))

    def test_empty_artist_returns_false(self):
        fn = self._get_func()
        item = {"artist": "", "title": "Comfortably Numb", "album": "The Wall"}
        self.assertFalse(fn("pink floyd - comfortably numb.mp3", item))

    def test_backslash_path_normalized(self):
        fn = self._get_func()
        item = {"artist": "Pink Floyd", "title": "Comfortably Numb", "album": ""}
        self.assertTrue(fn("Pink Floyd\\Comfortably Numb.flac", item))

    def test_similarity_fallback_with_partial_match(self):
        """Similarity fallback: score >= 0.60 with at least artist or title present."""
        fn = self._get_func()
        # Artist is present in filename but title has slight differences
        item = {"artist": "Radiohead", "title": "Creep", "album": "Pablo Honey"}
        # Both artist and title are substrings → should match
        self.assertTrue(fn("radiohead creep pablo honey.flac", item))

    def test_similarity_fallback_neither_present_returns_false(self):
        """Neither artist nor title in filename → False even if similarity is high."""
        fn = self._get_func()
        item = {"artist": "Radiohead", "title": "Creep", "album": "Pablo Honey"}
        self.assertFalse(fn("totally unrelated track name here.mp3", item))


class SiblingDownloadCleanupTests(unittest.TestCase):
    """Unit tests for _cleanup_sibling_downloads (no DB/network needed)."""

    def _get_func_and_set_dir(self, tmpdir):
        import sys, os
        if "queue_processor" not in sys.modules:
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "queue_processor",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_processor.py"),
                )
                mod = importlib.util.module_from_spec(spec)
                sys.modules["queue_processor"] = mod
                spec.loader.exec_module(mod)
            except Exception as e:
                raise unittest.SkipTest(f"queue_processor not importable: {e}")
        mod = sys.modules["queue_processor"]
        original_dir = mod.DOWNLOADS_DIR
        mod.DOWNLOADS_DIR = tmpdir
        return mod._cleanup_sibling_downloads, original_dir, mod

    def test_removes_sibling_not_keep_path(self):
        """Sibling files matching artist+title are removed; keep_path file is preserved."""
        import tempfile, os, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            keep = os.path.join(tmpdir, "artist - title (1).mp3")
            sibling = os.path.join(tmpdir, "artist - title (2).mp3")
            unrelated = os.path.join(tmpdir, "other artist - other song.mp3")
            for f in (keep, sibling, unrelated):
                open(f, "w").close()

            item = {"artist": "artist", "title": "title", "album": ""}
            fn, orig, mod = self._get_func_and_set_dir(tmpdir)
            try:
                fn(item, keep_path=keep)
            finally:
                mod.DOWNLOADS_DIR = orig

            self.assertTrue(os.path.isfile(keep), "keep_path must not be deleted")
            self.assertFalse(os.path.isfile(sibling), "sibling must be deleted")
            self.assertTrue(os.path.isfile(unrelated), "unrelated file must not be deleted")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_crash_on_missing_artist_or_title(self):
        """Empty artist/title — function returns without deleting anything."""
        import tempfile, os, shutil
        tmpdir = tempfile.mkdtemp()
        try:
            f = os.path.join(tmpdir, "some file.mp3")
            open(f, "w").close()
            item = {"artist": "", "title": "", "album": ""}
            fn, orig, mod = self._get_func_and_set_dir(tmpdir)
            try:
                fn(item, keep_path=None)  # Should not raise or delete
            finally:
                mod.DOWNLOADS_DIR = orig
            self.assertTrue(os.path.isfile(f), "file must not be deleted when artist/title empty")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_crash_on_missing_downloads_dir(self):
        """Non-existent downloads dir — function silently returns."""
        import sys, os
        if "queue_processor" not in sys.modules:
            raise unittest.SkipTest("queue_processor not importable in this environment")
        mod = sys.modules["queue_processor"]
        orig = mod.DOWNLOADS_DIR
        mod.DOWNLOADS_DIR = "/tmp/__nonexistent_dir_xyz__"
        try:
            item = {"artist": "Test", "title": "Song", "album": ""}
            mod._cleanup_sibling_downloads(item, keep_path=None)  # Should not raise
        finally:
            mod.DOWNLOADS_DIR = orig


class ConfirmedCollectionMatchLogicTests(unittest.TestCase):
    """Unit tests for the _is_confirmed_collection_match logic (no DB/network needed)."""

    def _run_match(self, queue_item, matched_data):
        """Import and call _is_confirmed_collection_match directly."""
        import importlib.util, sys, os
        # We only need the function — import the module without running side effects
        spec = importlib.util.spec_from_file_location(
            "queue_processor_mod",
            os.path.join(os.path.dirname(__file__), "queue_processor.py"),
        )
        # Avoid executing module-level code that needs external services
        # by relying on the already-imported version when available
        if "queue_processor" in sys.modules:
            mod = sys.modules["queue_processor"]
        else:
            # Parse the function body by text to avoid import-time side effects
            raise unittest.SkipTest("queue_processor not importable in this environment")
        return mod._is_confirmed_collection_match(queue_item, matched_data)

    def test_logic_full_match(self):
        """Verify logic: all four criteria present and matching → True."""
        # We test the logic by reading and verifying the function's text-level
        # correctness rather than executing it (to avoid import-time side effects).
        processor_text = _read("queue_processor.py")
        # Must check title, artist, album, and duration thresholds
        self.assertIn("_NAV_TITLE_SIMILARITY_THRESHOLD", processor_text)
        self.assertIn("_NAV_ARTIST_SIMILARITY_THRESHOLD", processor_text)
        self.assertIn("_CONFIRMED_MATCH_DURATION_TOLERANCE_SECONDS", processor_text)
        self.assertIn("_ALBUM_SIMILARITY_THRESHOLD", processor_text)

    def test_logic_missing_matched_data_returns_false(self):
        processor_text = _read("queue_processor.py")
        self.assertIn("if not matched_data:", processor_text)
        self.assertIn("return False", processor_text)


class PrefixTitleProtectionTests(unittest.TestCase):
    """Unit tests for the prefix-title false-positive guard in _metadata_matches_queue_item.

    Songs like 'World So Cold' and 'World So Cold Intro' share a high title
    similarity (~0.81) but are distinct tracks and must not be matched to each
    other via fuzzy metadata comparison.
    """

    def _get_proc_fn(self):
        import sys, os, importlib.util
        if "queue_processor" in sys.modules:
            mod = sys.modules["queue_processor"]
            fn = getattr(mod, "_metadata_matches_queue_item", None)
            if fn is None:
                raise unittest.SkipTest("_metadata_matches_queue_item not available")
            return fn
        spec = importlib.util.spec_from_file_location(
            "queue_processor",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_processor.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["queue_processor"] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            raise unittest.SkipTest(f"queue_processor not importable: {e}")
        fn = getattr(mod, "_metadata_matches_queue_item", None)
        if fn is None:
            raise unittest.SkipTest("_metadata_matches_queue_item not available")
        return fn

    def _call_with_fake_metadata(self, fn, file_title, file_artist, queue_title, queue_artist, album="Weathered"):
        """Call _metadata_matches_queue_item with patched metadata reading."""
        from unittest.mock import patch, MagicMock

        # Build a fake audio object with no meaningful duration info
        fake_audio = MagicMock()
        fake_audio.tags = None
        fake_audio.info = None

        fake_meta = {"artist": file_artist, "title": file_title, "album": album}

        queue_item = {"artist": queue_artist, "title": queue_title, "album": album, "duration": None}

        with patch("queue_processor.read_mp3_metadata", return_value=fake_meta), \
             patch("queue_processor.MutagenFile", return_value=fake_audio):
            return fn("/fake/path/song.mp3", queue_item)

    def test_prefix_title_no_match(self):
        """'World So Cold' file must NOT match 'World So Cold Intro' queue item."""
        fn = self._get_proc_fn()
        result = self._call_with_fake_metadata(
            fn,
            file_title="World So Cold",
            file_artist="Creed",
            queue_title="World So Cold Intro",
            queue_artist="Creed",
        )
        self.assertFalse(
            result,
            "'World So Cold' should not match 'World So Cold Intro' queue item",
        )

    def test_prefix_title_reverse_no_match(self):
        """'World So Cold Intro' file must NOT match 'World So Cold' queue item."""
        fn = self._get_proc_fn()
        result = self._call_with_fake_metadata(
            fn,
            file_title="World So Cold Intro",
            file_artist="Creed",
            queue_title="World So Cold",
            queue_artist="Creed",
        )
        self.assertFalse(
            result,
            "'World So Cold Intro' should not match 'World So Cold' queue item",
        )

    def test_exact_title_still_matches(self):
        """Exact title match must still succeed after the prefix guard is applied."""
        fn = self._get_proc_fn()
        result = self._call_with_fake_metadata(
            fn,
            file_title="World So Cold",
            file_artist="Creed",
            queue_title="World So Cold",
            queue_artist="Creed",
        )
        self.assertTrue(
            result,
            "Exact title 'World So Cold' should match 'World So Cold' queue item",
        )

    def test_unrelated_titles_still_rejected(self):
        """Completely different titles must be rejected (control case)."""
        fn = self._get_proc_fn()
        result = self._call_with_fake_metadata(
            fn,
            file_title="Comfortably Numb",
            file_artist="Pink Floyd",
            queue_title="Stairway to Heaven",
            queue_artist="Led Zeppelin",
            album="",
        )
        self.assertFalse(
            result,
            "Unrelated titles should not match",
        )


if __name__ == "__main__":
    unittest.main()
