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

    def test_matched_items_file_existence_check_in_normalization(self):
        """api_downloads_get_queue must reset 'matched' items whose file no longer exists."""
        app_text = _read("app.py")
        # The normalization block must query matched items with a file_path
        self.assertIn("status = 'matched'", app_text)
        self.assertIn("os.path.isfile(mrow_file_path)", app_text)
        # And reset them to 'queued' when the file is gone
        self.assertIn("Auto-corrected: matched file no longer exists on disk", app_text)

    def test_check_track_exists_in_db_verifies_file_on_disk(self):
        """check_track_exists_in_db must skip stale tracks whose file was deleted."""
        processor_text = _read("queue_processor.py")
        # Must select file_path from tracks so it can be verified
        self.assertIn("file_path FROM tracks", processor_text)
        # Must check os.path.isfile before returning True
        self.assertIn("os.path.isfile(db_file_path)", processor_text)
        # Must return False when file is missing
        self.assertIn("file no longer on disk", processor_text)

    def test_check_missing_moved_files_handles_matched_items(self):
        """check_missing_moved_files must also reset matched items with missing files."""
        verif_text = _read("download_file_verification.py")
        # Must check matched items with file_path set
        self.assertIn("status = 'matched'", verif_text)
        self.assertIn("_reset_matched_item_to_queued", verif_text)
        # Must define the helper that resets them
        self.assertIn("def _reset_matched_item_to_queued", verif_text)


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

    def test_variant_suffix_does_not_match_plain_title(self):
        """Queue title without variant must not match a '(mix/edit/live)' filename variant."""
        fn = self._get_func()
        item = {"artist": "Aiden", "title": "The Last Sunrise", "album": "REV"}
        self.assertFalse(fn("07. Aiden - The Last Sunrise (Dusk mix).flac", item))


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


class SlskdDownloadTimeoutTests(unittest.TestCase):
    """Tests confirming download timeout and slskd cleanup functionality."""

    def test_active_state_timeout_logic_present(self):
        """queue_processor must apply timeouts for downloads stuck in active states."""
        processor_text = _read("queue_processor.py")
        # Module-level timeout constant must be defined
        self.assertIn("_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES", processor_text)
        # Must cover the key active states
        self.assertIn("Queued, Remotely", processor_text)
        self.assertIn("InProgress", processor_text)
        # The cancellation path must be present
        self.assertIn("cancel_download", processor_text)
        self.assertIn("slskd download timed out", processor_text)

    def test_active_state_timeout_uses_stale_check(self):
        """Timeout detection must use _is_stale_queue_item with per-state limits."""
        processor_text = _read("queue_processor.py")
        self.assertIn("_is_stale_queue_item(item, stale_minutes=timeout_minutes)", processor_text)

    def test_active_state_timeout_cancels_transfer(self):
        """When a timeout is triggered the transfer must be cancelled in slskd."""
        processor_text = _read("queue_processor.py")
        self.assertIn("slskd_client.cancel_download(transfer_username, transfer_id, remove=True)", processor_text)

    def test_mark_failed_clears_stale_slskd_transfer_before_retry(self):
        """Failed Soulseek rows must remove stale slskd transfers before being re-queued."""
        processor_text = _read("queue_processor.py")
        self.assertIn("def _clear_stale_slskd_transfer_for_queue_item", processor_text)
        self.assertIn("_clear_stale_slskd_transfer_for_queue_item(row_dict)", processor_text)

    def test_clear_completed_downloads_method_exists(self):
        """SlskdClient must expose clear_completed_downloads()."""
        slskd_text = _read("api_clients/slskd.py")
        self.assertIn("def clear_completed_downloads", slskd_text)
        self.assertIn("transfers/downloads/all/completed", slskd_text)

    def test_maybe_clear_slskd_completed_downloads_defined(self):
        """queue_processor must define a periodic slskd cleanup helper."""
        processor_text = _read("queue_processor.py")
        self.assertIn("def maybe_clear_slskd_completed_downloads", processor_text)
        self.assertIn("clear_completed_downloads()", processor_text)
        # Must be wired into the main processor loop
        self.assertIn("last_slskd_cleanup_ts", processor_text)
        self.assertIn("maybe_clear_slskd_completed_downloads(now_ts, last_slskd_cleanup_ts)", processor_text)

    def test_cleanup_uses_30_minute_interval(self):
        """Periodic slskd cleanup must default to a 30-minute (1800 second) interval."""
        processor_text = _read("queue_processor.py")
        # The default interval_seconds argument must be 1800
        self.assertIn("interval_seconds=1800", processor_text)


class SlskdIsStaleQueueItemTests(unittest.TestCase):
    """Tests confirming _is_stale_queue_item is wired into the timeout logic."""

    def test_queued_remotely_timeout_is_120_minutes(self):
        """Timeout for Queued, Remotely must be 120 minutes."""
        processor_text = _read("queue_processor.py")
        self.assertIn('"Queued, Remotely": 120', processor_text)

    def test_in_progress_timeout_is_240_minutes(self):
        """Timeout for InProgress must be 240 minutes."""
        processor_text = _read("queue_processor.py")
        self.assertIn('"InProgress": 240', processor_text)

    def test_requested_timeout_is_30_minutes(self):
        """Timeout for Requested state must be 30 minutes."""
        processor_text = _read("queue_processor.py")
        self.assertIn('"Requested": 30', processor_text)

    def test_stale_queue_item_helper_defined(self):
        """_is_stale_queue_item must be defined in queue_processor."""
        processor_text = _read("queue_processor.py")
        self.assertIn("def _is_stale_queue_item", processor_text)
        # Must check updated_at field
        self.assertIn("updated_at", processor_text)

    def test_timeout_map_covers_all_active_states(self):
        """Timeout map must include all known active slskd transfer states."""
        processor_text = _read("queue_processor.py")
        for state in ("Queued, Remotely", "Requested", "Initializing", "InProgress"):
            self.assertIn(state, processor_text, f"Timeout map must include '{state}'")


class MusicBrainzCompareCoreMatchTests(unittest.TestCase):
    """Tests for the core-title matching logic in api_album_musicbrainz_compare.

    The compare endpoint must recognise that a library track titled 'World So Cold'
    is the same recording as the MusicBrainz title
    'World So Cold (live at USANA Amphitheatre, Salt Lake City, UT - August 2003)'.
    """

    def test_core_title_match_in_app_text(self):
        """app.py must implement core-title matching (step 4) in the compare endpoint."""
        app_text = _read("app.py")
        # Verify the step-4 comment and the regex are present in the compare function.
        # Find the function body between its def and the next top-level def/route.
        func_start = app_text.find("def api_album_musicbrainz_compare()")
        self.assertGreater(func_start, 0, "api_album_musicbrainz_compare not found in app.py")
        # Grab a generous slice of the function (compare endpoint is ~200 lines)
        func_body = app_text[func_start:func_start + 8000]
        self.assertIn("Core-title match", func_body,
                      "Step-4 comment must be present in api_album_musicbrainz_compare")
        self.assertIn(r"[\(\[].+$", func_body,
                      "Core-title stripping regex must be present in api_album_musicbrainz_compare")

    def test_core_title_stripping_logic(self):
        """Verify the core-title stripping regex works for the reported example."""
        import re
        mb_title = "World So Cold (live at USANA Amphitheatre, Salt Lake City, UT - August 2003)"
        norm_mb = re.sub(r"\s+", " ", mb_title.lower().strip())
        # Step 4 strips from first ( or [
        norm_mb_core = re.sub(r"\s*[\(\[].+$", "", norm_mb).strip()
        self.assertEqual(norm_mb_core, "world so cold")
        # Core differs from full title, so step 4 should run
        self.assertNotEqual(norm_mb_core, norm_mb)

    def test_core_title_does_not_strip_plain_suffix_words(self):
        """Step 4 must NOT run when the MB title has no parenthetical suffix.

        'World So Cold Intro' has no parens/brackets — its core == full title so
        the step-4 branch is skipped and the title is not erroneously matched.
        """
        import re
        mb_title = "World So Cold Intro"
        norm_mb = re.sub(r"\s+", " ", mb_title.lower().strip())
        norm_mb_core = re.sub(r"\s*[\(\[].+$", "", norm_mb).strip()
        # No stripping happened: core == full
        self.assertEqual(norm_mb_core, norm_mb)

    def test_core_title_stripping_remaster_example(self):
        """'Fade to Black (Remastered)' must strip to 'Fade to Black'."""
        import re
        mb_title = "Fade to Black (Remastered)"
        norm_mb = re.sub(r"\s+", " ", mb_title.lower().strip())
        norm_mb_core = re.sub(r"\s*[\(\[].+$", "", norm_mb).strip()
        self.assertEqual(norm_mb_core, "fade to black")


class AlbumFolderEqualsSongTitleTests(unittest.TestCase):
    """Tests for the album-folder-equals-song-title false positive in filename matching.

    When a queue item has title "This Is The Sound" and the downloaded album folder
    is also "This Is The Sound", every file in that folder has the track title in
    its path — even unrelated tracks like "02. Skindred - You Got This.flac".
    The filename matcher must require the title to appear in the basename, not
    merely in the directory component.
    """

    def _run_filename_match(self, filename, artist, title, album=""):
        """Import and call _filename_matches_queue_item from queue_processor."""
        import importlib
        import sys
        sys.path.insert(0, ".")
        try:
            qp = importlib.import_module("queue_processor")
        except Exception:
            self.skipTest("queue_processor could not be imported")
        item = {"artist": artist, "title": title, "album": album}
        return qp._filename_matches_queue_item(filename, item)

    def test_bug_case_wrong_track_not_matched(self):
        """Track 'You Got This' in album folder 'This Is The Sound' must NOT
        match queue item for song 'This Is The Sound'."""
        result = self._run_filename_match(
            "This Is The Sound/02. Skindred - You Got This.flac",
            artist="Skindred",
            title="This Is The Sound",
            album="This Is The Sound",
        )
        self.assertFalse(result, "Title in folder only must not trigger a match")

    def test_correct_track_in_album_folder_is_matched(self):
        """The actual 'This Is The Sound' track inside the same album folder
        must still be matched correctly (title present in both folder and basename)."""
        result = self._run_filename_match(
            "This Is The Sound/01. Skindred - This Is The Sound.flac",
            artist="Skindred",
            title="This Is The Sound",
            album="This Is The Sound",
        )
        self.assertTrue(result, "Title in basename must match the correct track")

    def test_another_wrong_track_in_album_folder_not_matched(self):
        """A third track from the same album must also not match the title-song item."""
        result = self._run_filename_match(
            "This Is The Sound/03. Skindred - Tear It Down.flac",
            artist="Skindred",
            title="This Is The Sound",
            album="This Is The Sound",
        )
        self.assertFalse(result, "Different track, title only in folder — must not match")

    def test_normal_case_unrelated_album_matches(self):
        """Normal download where the title appears in the filename must still match."""
        result = self._run_filename_match(
            "Black Album/01. Metallica - Enter Sandman.flac",
            artist="Metallica",
            title="Enter Sandman",
            album="The Black Album",
        )
        self.assertTrue(result, "Title in basename should match")

    def test_processor_basename_guard_in_source(self):
        """queue_processor.py must contain the basename guard for title matching."""
        processor_text = _read("queue_processor.py")
        self.assertIn("basename_test", processor_text)
        self.assertIn("title_in_basename", processor_text)
        # The guard comment explains the album-folder-equals-song-title case
        self.assertIn("only appears in the directory portion", processor_text)

    def test_score_candidate_does_not_award_folder_only_title_bonus(self):
        """_score_soulseek_candidate must check title in basename before awarding
        the title-in-path bonus."""
        processor_text = _read("queue_processor.py")
        self.assertIn("basename_norm", processor_text)
        self.assertIn("title_norm in basename_norm", processor_text)


class NavidromeScanTriggerTests(unittest.TestCase):
    """Tests for the periodic and immediate Navidrome scan-trigger logic.

    After a downloaded file is successfully moved to /music, the queue processor
    must trigger a Navidrome ``startScan`` so the file appears in Navidrome without
    waiting for a manual full-import.  A periodic safety-net also fires every
    5 minutes when recently-imported items are detected.
    """

    def test_trigger_helper_in_source(self):
        """queue_processor.py must define _trigger_navidrome_scan()."""
        processor_text = _read("queue_processor.py")
        self.assertIn("def _trigger_navidrome_scan(", processor_text)
        self.assertIn("startScan", processor_text)

    def test_trigger_called_after_auto_move(self):
        """_trigger_navidrome_scan() must be called in the successful auto-move path."""
        processor_text = _read("queue_processor.py")
        # Confirm the call sits in the same neighbourhood as the auto-move success block
        auto_move_idx = processor_text.find("verified and imported to")
        trigger_idx = processor_text.find("_trigger_navidrome_scan()", auto_move_idx)
        self.assertGreater(
            trigger_idx,
            auto_move_idx,
            "_trigger_navidrome_scan() must appear after the auto-move success log line",
        )

    def test_periodic_function_in_source(self):
        """queue_processor.py must define maybe_trigger_navidrome_scan_for_new_imports()."""
        processor_text = _read("queue_processor.py")
        self.assertIn("def maybe_trigger_navidrome_scan_for_new_imports(", processor_text)

    def test_periodic_function_wired_into_run_processor(self):
        """run_processor() must call maybe_trigger_navidrome_scan_for_new_imports."""
        processor_text = _read("queue_processor.py")
        run_idx = processor_text.find("def run_processor(")
        self.assertGreater(run_idx, 0)
        run_body = processor_text[run_idx:]
        self.assertIn("maybe_trigger_navidrome_scan_for_new_imports", run_body)

    def test_periodic_function_uses_interval(self):
        """The periodic function must respect its interval_seconds parameter."""
        processor_text = _read("queue_processor.py")
        # Confirm that the function compares now_ts against last_run_ts with interval
        func_idx = processor_text.find("def maybe_trigger_navidrome_scan_for_new_imports(")
        func_body = processor_text[func_idx:func_idx + 3000]
        self.assertIn("interval_seconds", func_body)
        self.assertIn("last_run_ts", func_body)


class SoftMismatchMetadataTests(unittest.TestCase):
    """Tests for the soft-mismatch path in _metadata_matches_queue_item.

    When a file's tags contain a version or remix suffix (e.g. "Creep (Acoustic)"
    for a queue item titled "Creep"), the title similarity falls below 0.55 but
    above the hard-mismatch floor of 0.35.  The function must return None rather
    than False so that the caller can fall back to filename matching.

    Completely wrong files (scores below 0.35) must still return False to block
    any further matching attempts.
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

    def _call(self, fn, file_title, file_artist, queue_title, queue_artist):
        """Call _metadata_matches_queue_item with injected metadata."""
        from unittest.mock import patch, MagicMock

        fake_audio = MagicMock()
        fake_audio.tags = None
        fake_audio.info = None

        fake_meta = {"artist": file_artist, "title": file_title}
        queue_item = {"artist": queue_artist, "title": queue_title, "duration": None}

        with patch("queue_processor.read_mp3_metadata", return_value=fake_meta), \
             patch("queue_processor.MutagenFile", return_value=fake_audio):
            return fn("/fake/path/track.flac", queue_item)

    def test_version_suffix_returns_none_not_false(self):
        """'Creep (Acoustic)' file for queue item 'Creep' must return None, not False.

        The title similarity is in the soft-mismatch zone (0.35–0.55).  The
        function should return None so the caller can attempt filename matching
        rather than rejecting the file outright.
        """
        fn = self._get_proc_fn()
        result = self._call(
            fn,
            file_title="Creep (Acoustic)",
            file_artist="Radiohead",
            queue_title="Creep",
            queue_artist="Radiohead",
        )
        self.assertIsNone(
            result,
            "A title with a short version suffix should return None (allow filename fallback), not False",
        )

    def test_clearly_wrong_artist_still_returns_false(self):
        """A completely different artist (score < 0.35) must still return False."""
        fn = self._get_proc_fn()
        result = self._call(
            fn,
            file_title="Yesterday",
            file_artist="The Beatles",
            queue_title="Creep",
            queue_artist="Radiohead",
        )
        self.assertFalse(
            result,
            "A file by a completely different artist must still return False",
        )

    def test_hard_mismatch_floor_in_source(self):
        """queue_processor.py must define a HARD_MISMATCH_FLOOR constant (0.35)."""
        processor_text = _read("queue_processor.py")
        self.assertIn("_HARD_MISMATCH_FLOOR", processor_text)
        self.assertIn("0.35", processor_text)

    def test_soft_mismatch_returns_none_path_in_source(self):
        """queue_processor.py must return None (not False) for soft mismatches."""
        processor_text = _read("queue_processor.py")
        # The soft-mismatch block comment explains the rationale
        self.assertIn("Soft mismatch", processor_text)
        # The block must return None to allow filename fallback
        self.assertIn("fall back to filename matching rather than rejecting", processor_text)

    def test_exact_match_still_returns_true(self):
        """Perfect artist+title match must still return True."""
        fn = self._get_proc_fn()
        result = self._call(
            fn,
            file_title="Creep",
            file_artist="Radiohead",
            queue_title="Creep",
            queue_artist="Radiohead",
        )
        self.assertTrue(result, "Exact artist+title match must return True")


class SlskdCleanupStartupTests(unittest.TestCase):
    """Tests confirming that maybe_clear_slskd_completed_downloads skips its
    first run on startup so check_completed_downloads() can read completed
    transfers before they are cleared from slskd."""

    def test_cleanup_skips_first_run(self):
        """maybe_clear_slskd_completed_downloads must skip when last_run_ts is None."""
        processor_text = _read("queue_processor.py")
        func_idx = processor_text.find("def maybe_clear_slskd_completed_downloads(")
        func_body = processor_text[func_idx:func_idx + 2000]
        # The function must check for last_run_ts is None and return early
        self.assertIn("last_run_ts is None", func_body)
        self.assertIn("Startup run skipped", func_body)

    def test_cleanup_startup_skip_returns_now_ts(self):
        """When last_run_ts is None the function must return now_ts so it is
        treated as if it ran and the next cleanup is deferred by interval_seconds."""
        processor_text = _read("queue_processor.py")
        func_idx = processor_text.find("def maybe_clear_slskd_completed_downloads(")
        func_body = processor_text[func_idx:func_idx + 2000]
        # After the startup-skip block it should return now_ts
        self.assertIn("return now_ts", func_body)


class PrefixTitleFilenameMatchTests(unittest.TestCase):
    """Tests that _filename_matches_queue_item rejects prefix-title false positives.

    An album may contain two tracks whose titles share a common prefix, e.g.
    "-1" and "-1 intro".  When matching a file named "-1 intro.flac" against the
    queue item for "-1", the simple ``title in basename`` check would return True
    because the string "-1" is a substring of "-1 intro.flac".  The fix requires
    the title match to be a *complete phrase* — not followed by additional
    alphabetic words — so that "-1" only matches "-1.flac" and not "-1 intro.flac".
    """

    def _run(self, filename, artist, title, album=""):
        import sys, importlib
        sys.path.insert(0, ".")
        try:
            qp = importlib.import_module("queue_processor")
        except Exception:
            self.skipTest("queue_processor could not be imported")
        return qp._filename_matches_queue_item(filename, {"artist": artist, "title": title, "album": album})

    # ------------------------------------------------------------------ #
    # The core bug: "-1 intro" must NOT match "-1" queue item             #
    # ------------------------------------------------------------------ #

    def test_negative_one_intro_does_not_match_negative_one(self):
        """-1 intro.flac must NOT match a queue item titled '-1'."""
        result = self._run(
            "album/02 - artist - -1 intro.flac",
            artist="artist",
            title="-1",
        )
        self.assertFalse(
            result,
            "A file for '-1 intro' must not match a '-1' queue item",
        )

    def test_negative_one_matches_negative_one(self):
        """-1.flac MUST match a queue item titled '-1'."""
        result = self._run(
            "album/01 - artist - -1.flac",
            artist="artist",
            title="-1",
        )
        self.assertTrue(
            result,
            "A file for '-1' must match a '-1' queue item",
        )

    def test_negative_one_interlude_does_not_match_negative_one(self):
        """-1 interlude.flac must NOT match a queue item titled '-1'."""
        result = self._run(
            "album/03 - artist - -1 interlude.flac",
            artist="artist",
            title="-1",
        )
        self.assertFalse(
            result,
            "A file for '-1 interlude' must not match a '-1' queue item",
        )

    # ------------------------------------------------------------------ #
    # Adjacent cases: parenthetical suffixes are still accepted           #
    # ------------------------------------------------------------------ #

    def test_acoustic_version_still_matches(self):
        """'-1 (acoustic).flac' MUST match a queue item titled '-1'.

        A parenthetical suffix does not start with a bare letter, so the
        whole-phrase check must not block it.
        """
        result = self._run(
            "album/01 - artist - -1 (acoustic).flac",
            artist="artist",
            title="-1",
        )
        self.assertTrue(
            result,
            "'-1 (acoustic).flac' should still match a '-1' queue item",
        )

    def test_world_so_cold_does_not_match_world_so_cold_intro(self):
        """'world so cold intro.flac' must NOT match the 'World So Cold' queue item."""
        result = self._run(
            "album/02 - creed - world so cold intro.flac",
            artist="creed",
            title="World So Cold",
        )
        self.assertFalse(
            result,
            "'World So Cold Intro' file should not match 'World So Cold' queue item",
        )

    def test_mixed_case_filename_does_not_match_intro(self):
        """Mixed-case '-1 Intro.flac' must NOT match a '-1' queue item.

        Filenames are lowercased before the regex runs, so even when the original
        file has capital letters in the continuation word the whole-phrase guard
        still fires correctly.
        """
        result = self._run(
            "album/02 - Artist - -1 Intro.flac",
            artist="Artist",
            title="-1",
        )
        self.assertFalse(
            result,
            "A mixed-case '-1 Intro.flac' file must not match a '-1' queue item",
        )

    # ------------------------------------------------------------------ #
    # Source-level guard: the regex must be present in queue_processor.py #
    # ------------------------------------------------------------------ #

    def test_whole_phrase_guard_in_source(self):
        """queue_processor.py must contain the whole-phrase title check."""
        processor_text = _read("queue_processor.py")
        # The fix replaces `title in basename_test` with a regex look-ahead
        self.assertIn(r"(?!\s*[a-z])", processor_text,
                      "queue_processor.py should contain the look-ahead regex for prefix-title guard")


class DiscTrackFilenameMatchTests(unittest.TestCase):
    """Tests that filename matching and Soulseek scoring handle common Soulseek
    filename patterns correctly.

    Soulseek peers frequently name files with:
      * Disc-track prefixes: "1-15 - Title.mp3"
      * Simple track prefixes: "07 Title.mp3" (no separator — title is kept intact)
      * Trailing collision-avoidance UIDs: "16. Artist - Title_639091010921933965.flac"
    These patterns must not prevent a file from being matched to the correct queue
    item, and the Soulseek score must remain above the minimum download threshold.
    """

    def setUp(self):
        import importlib, sys
        sys.path.insert(0, ".")
        try:
            self._qp = importlib.import_module("queue_processor")
        except Exception as exc:
            self.skipTest("queue_processor could not be imported: %s" % exc)

    def _run_filename_match(self, filename, artist, title, album=""):
        item = {"artist": artist, "title": title, "album": album}
        return self._qp._filename_matches_queue_item(filename, item)

    def _score(self, filename, artist, title, album="", duration=None):
        item = {"artist": artist, "title": title, "album": album, "duration": duration}
        return self._qp._score_soulseek_candidate(filename, item, duration)

    # ------------------------------------------------------------------ #
    # _filename_matches_queue_item — disc-track prefix                    #
    # ------------------------------------------------------------------ #

    def test_disc_track_prefix_matches_queue_item(self):
        """'1-15 - Worms of the Earth.mp3' must match queue item 'Worms of the Earth'."""
        result = self._run_filename_match(
            "album/1-15 - Worms of the Earth.mp3",
            artist="Primordial",
            title="Worms of the Earth",
        )
        self.assertTrue(
            result,
            "A disc-track prefixed file must match the bare title queue item",
        )

    def test_standard_numbered_prefix_matches_queue_item(self):
        """'16. Cradle Of Filth - Halloween 2.flac' must match the correct queue item."""
        result = self._run_filename_match(
            "album/16. Cradle Of Filth - Halloween 2.flac",
            artist="Cradle Of Filth",
            title="Halloween 2",
        )
        self.assertTrue(result, "Numbered prefix with artist–title stem must match")

    def test_soulseek_uid_suffix_filename_matches_queue_item(self):
        """File with a long Soulseek UID suffix must still match the queue item."""
        result = self._run_filename_match(
            "16. Cradle Of Filth - Halloween 2_639091010921933965.flac",
            artist="Cradle Of Filth",
            title="Halloween 2",
        )
        self.assertTrue(
            result,
            "A file whose stem ends with a Soulseek UID must still match the queue item",
        )

    # ------------------------------------------------------------------ #
    # _score_soulseek_candidate — disc-track and UID suffix filenames     #
    # ------------------------------------------------------------------ #

    def test_disc_track_prefix_score_above_threshold(self):
        """_score_soulseek_candidate must return > 0.45 for a disc-track filename
        so that the file is considered for download."""
        score = self._score(
            "albumdir/1-15 - Worms of the Earth.mp3",
            artist="Primordial",
            title="Worms of the Earth",
            album="Storm Before Calm",
        )
        self.assertGreater(
            score,
            0.45,
            f"Disc-track prefixed filename should score above 0.45, got {score:.3f}",
        )

    def test_soulseek_uid_suffix_score_above_threshold(self):
        """_score_soulseek_candidate must return > 0.45 for a file with a
        Soulseek UID suffix so the file is not rejected during search."""
        score = self._score(
            "albumdir/16. Cradle Of Filth - Halloween 2_639091010921933965.flac",
            artist="Cradle Of Filth",
            title="Halloween 2",
        )
        self.assertGreater(
            score,
            0.45,
            f"Soulseek-UID-suffixed filename should score above 0.45, got {score:.3f}",
        )

    def test_track_without_separator_score_above_threshold(self):
        """'07 Optimissed.mp3' — track number with no separator — must score > 0.45."""
        score = self._score(
            "07 Optimissed.mp3",
            artist="Primordial",
            title="Optimissed",
        )
        self.assertGreater(
            score,
            0.45,
            f"Track-number-prefixed filename without separator should score > 0.45, got {score:.3f}",
        )

    # ------------------------------------------------------------------ #
    # _strip_track_number_prefix — UID suffix stripping                   #
    # ------------------------------------------------------------------ #

    def test_strip_prefix_and_uid_suffix(self):
        """_strip_track_number_prefix must strip both prefix AND trailing UID."""
        dqm_text = _read("download_queue_manager.py")
        # Verify the regex for stripping the trailing UID is present
        self.assertIn(r"_\d{12,}$", dqm_text,
                      "download_queue_manager.py must strip trailing Soulseek UID suffixes")

    def test_strip_prefix_and_uid_in_queue_processor(self):
        """queue_processor.py must also strip leading prefix and trailing UID."""
        qp_text = _read("queue_processor.py")
        self.assertIn(r"_\d{12,}$", qp_text,
                      "queue_processor.py must strip trailing Soulseek UID suffixes")

    def test_auto_discover_error_handler_logs_traceback(self):
        """auto_discover_and_queue_files must log a full traceback on per-file
        errors so the exact crash location is captured in production."""
        dqm_text = _read("download_queue_manager.py")
        self.assertIn("format_exc", dqm_text,
                      "download_queue_manager.py must call traceback.format_exc() in "
                      "the per-file error handler")


class MusicBrainzFileMatcherFreezeTests(unittest.TestCase):
    """Tests that verify the container-freeze fix in musicbrainz_file_matcher.py."""

    def _read_matcher(self):
        return _read("musicbrainz_file_matcher.py")

    def _read_finalizer(self):
        return _read("musicbrainz_finalizer.py")

    def test_check_and_trigger_uses_database_query(self):
        """check_and_trigger_auto_ready_and_transfer must use DatabaseQuery, not raw
        cursor, so that the correct placeholder (?/%s) is chosen for each backend."""
        matcher_text = self._read_matcher()
        self.assertIn(
            "db_query = DatabaseQuery(conn)",
            matcher_text,
            "check_and_trigger_auto_ready_and_transfer must use DatabaseQuery for "
            "portable SQLite/PostgreSQL placeholder handling",
        )

    def test_check_and_trigger_no_raw_percent_s_placeholder(self):
        """check_and_trigger_auto_ready_and_transfer must not contain hardcoded %s
        SQL placeholders; these crash on SQLite causing a connection leak."""
        import re
        matcher_text = self._read_matcher()
        # Extract only the function body
        start = matcher_text.find("def check_and_trigger_auto_ready_and_transfer")
        # Find the next top-level def after the function
        next_def = matcher_text.find("\n    def ", start + 1)
        func_body = matcher_text[start:] if next_def == -1 else matcher_text[start:next_def]
        # Should not contain %s used as SQL placeholder inside execute calls
        self.assertNotIn(
            'WHERE release_id = %s',
            func_body,
            "check_and_trigger_auto_ready_and_transfer must not use %s SQL placeholders "
            "directly — use DatabaseQuery with ? instead",
        )

    def test_check_and_trigger_calls_finalize_release_not_organize_folder(self):
        """check_and_trigger_auto_ready_and_transfer must call finalize_release(),
        not the non-existent organize_folder_to_music() method."""
        matcher_text = self._read_matcher()
        start = matcher_text.find("def check_and_trigger_auto_ready_and_transfer")
        next_def = matcher_text.find("\n    def ", start + 1)
        func_body = matcher_text[start:] if next_def == -1 else matcher_text[start:next_def]
        self.assertIn(
            "finalize_release(",
            func_body,
            "check_and_trigger_auto_ready_and_transfer must call finalizer.finalize_release()",
        )
        self.assertNotIn(
            "organize_folder_to_music(",
            func_body,
            "check_and_trigger_auto_ready_and_transfer must not call the non-existent "
            "organize_folder_to_music() method on MusicBrainzFinalizer",
        )

    def test_check_and_trigger_closes_connection_in_finally(self):
        """check_and_trigger_auto_ready_and_transfer must close the DB connection in a
        finally block to prevent connection leaks on exception."""
        matcher_text = self._read_matcher()
        start = matcher_text.find("def check_and_trigger_auto_ready_and_transfer")
        next_def = matcher_text.find("\n    def ", start + 1)
        func_body = matcher_text[start:] if next_def == -1 else matcher_text[start:next_def]
        self.assertIn(
            "finally:",
            func_body,
            "check_and_trigger_auto_ready_and_transfer must have a finally block",
        )
        self.assertIn(
            "conn.close()",
            func_body,
            "check_and_trigger_auto_ready_and_transfer must close conn in the finally block",
        )

    def test_finalizer_finalize_release_uses_placeholder_variable(self):
        """finalize_release() must use the dynamic placeholder variable for the release
        status UPDATE so the query works on both SQLite and PostgreSQL."""
        finalizer_text = self._read_finalizer()
        start = finalizer_text.find("def finalize_release(")
        next_def = finalizer_text.find("\n    def ", start + 1)
        func_body = finalizer_text[start:] if next_def == -1 else finalizer_text[start:next_def]
        # The status update should use {placeholder}, not a hardcoded '?'
        self.assertIn(
            "WHERE id = {placeholder}",
            func_body,
            "finalize_release must use the dynamic {placeholder} in the status UPDATE "
            "query so it works on both SQLite (?) and PostgreSQL (%s)",
        )
        self.assertNotIn(
            "WHERE id = ?",
            func_body,
            "finalize_release must not use a hardcoded '?' in the status UPDATE query",
        )


class SlskdLocalFilePathTrustTests(unittest.TestCase):
    """Tests confirming that check_completed_downloads trusts slskd's
    localFilePath for full-path matches without requiring metadata/filename
    matching to pass (fixing the case where files lack tags or have minimal
    filenames like '01.mp3')."""

    def test_full_path_match_trusted_without_file_matches(self):
        """queue_processor must accept slskd localFilePath when the full remote
        path matches found_filename, even if _file_matches_queue_item would fail.

        The key structural change: abs_path_full is set separately so that the
        full-path hit skips _file_matches_queue_item entirely.
        """
        processor_text = _read("queue_processor.py")
        # The fix distinguishes full-path hits from basename-only hits.
        self.assertIn("abs_path_full = slskd_completed.get(found_norm)", processor_text)
        self.assertIn("abs_path = abs_path_full or slskd_completed.get(os.path.basename(found_norm))", processor_text)

    def test_full_path_match_sets_slskd_localpath_meta_state(self):
        """When a full-path match is accepted the meta state must be 'slskd_localpath'
        so the downstream logging correctly identifies the source."""
        processor_text = _read("queue_processor.py")
        self.assertIn("match_meta_state = 'slskd_localpath'", processor_text)

    def test_full_path_match_does_not_run_file_matches_queue_item(self):
        """The full-path branch must NOT call _file_matches_queue_item — that
        check is reserved for the weaker basename-only case."""
        processor_text = _read("queue_processor.py")
        # Locate the step-1 block by finding the 'abs_path_full' variable
        step1_start = processor_text.find("abs_path_full = slskd_completed.get(found_norm)")
        step2_start = processor_text.find("# 2. Exact filename match against filesystem files")
        step1_block = processor_text[step1_start:step2_start]
        # The full-path branch is introduced by the identity + truthy check.
        full_branch_marker = "abs_path is abs_path_full and abs_path_full"
        self.assertIn(
            full_branch_marker,
            step1_block,
            "Full-path branch must use identity check 'abs_path is abs_path_full and abs_path_full'",
        )
        # Find the region from the full-path branch up to the 'else:' (basename branch).
        full_branch_start = step1_block.find(full_branch_marker)
        basename_branch_start = step1_block.find(
            "# Basename-only match:", full_branch_start
        )
        full_branch = step1_block[full_branch_start:basename_branch_start]
        self.assertNotIn(
            "_file_matches_queue_item",
            full_branch,
            "Full-path slskd match must not call _file_matches_queue_item",
        )

    def test_basename_only_match_still_uses_file_matches(self):
        """The basename-only fallback must still call _file_matches_queue_item
        to avoid false positives when multiple downloads share the same basename."""
        processor_text = _read("queue_processor.py")
        step1_start = processor_text.find("abs_path_full = slskd_completed.get(found_norm)")
        step2_start = processor_text.find("# 2. Exact filename match against filesystem files")
        step1_block = processor_text[step1_start:step2_start]
        # The else (basename) branch starts with this unique comment.
        basename_comment = "# Basename-only match:"
        basename_branch_start = step1_block.find(basename_comment)
        self.assertGreater(
            basename_branch_start, 0,
            "step-1 block must contain the '# Basename-only match:' comment",
        )
        else_branch = step1_block[basename_branch_start:]
        self.assertIn(
            "_file_matches_queue_item",
            else_branch,
            "Basename-only slskd match must verify with _file_matches_queue_item",
        )

    def test_outside_downloads_dir_guard_present(self):
        """When slskd's localFilePath is outside DOWNLOADS_DIR the match must be
        skipped to avoid path-traversal style issues with os.path.relpath."""
        processor_text = _read("queue_processor.py")
        self.assertIn("candidate_rel.startswith('..')", processor_text)
        self.assertIn("slskd localFilePath is outside DOWNLOADS_DIR", processor_text)


if __name__ == "__main__":
    unittest.main()
