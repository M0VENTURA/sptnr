"""Regression tests: the download-completion "no local file found" retry loop.

Reported bug (2026-08-29 logs): queue items downloaded successfully from
slskd ("Completed, Succeeded") but the completion service reported
"slskd succeeded but no local file found — retrying" EVERY cycle, each retry
searching and downloading a DIFFERENT file (Aephanemer "Utopie" tracks picked
"08 - Utopie (Partie I)", then "18 - Utopie (Partie II)", then "13
Par-dela...") — an infinite re-download loop.

Root causes fixed here:
1. ``_reconcile_transfer_state`` failed + cancelled a succeeded-but-unfound
   transfer IMMEDIATELY (the file may still be flushing/renaming), requeueing
   it and re-downloading a different file.  It now keeps the item
   ``downloading`` (returns False) and only fails it after a grace period.
2. ``check_completed_downloads`` processed STALE snapshot rows: a retry had
   already requeued + re-downloaded the item, but the old row (with the OLD
   ``found_filename``) was still failed again → double-fail.  Each item is
   now re-fetched fresh first.
3. ``_wait_for_transfer_file`` only walked 3 levels deep — enough for
   ``music/Artist/Album`` but the FILE lives one level deeper, so
   ``music/Artist/Album/01 - Track.flac`` was never found when the
   remote-join path drifted.  The walk is now deep (8 levels) and also
   searches the torrents dir / sibling ``torrents`` roots.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from services.downloads import download_completion_service as dcs


class _FakeSlskd:
    FAILED_STATES = frozenset(["Completed, Errored", "Completed, TimedOut"])
    ACTIVE_STATES = frozenset(["Requested", "Queued, Remotely", "InProgress"])
    STATE_QUEUED_REMOTELY = "Queued, Remotely"
    STATE_SUCCEEDED = "Completed, Succeeded"

    def __init__(self):
        self.cancelled = []

    @staticmethod
    def state_text(raw):
        return str(raw or "").strip()

    def is_success_state(self, raw):
        return self.state_text(raw) == self.STATE_SUCCEEDED

    def cancel_download(self, username, transfer_id, remove=True):
        self.cancelled.append((username, transfer_id, remove))
        return True


class TestPunctuationTolerantBasenameMatch:
    """The Aephanemer "Utopie" loop's real cause: the peer's remote filename
    uses a U+2010 hyphen + accents + parens (``Par‐delà le mur des siècles
    (instrumental)``) while the file lands on disk with a DIFFERENT peer's
    ASCII spelling (``Par-dela le mur des siecles instrumental``).  The
    basename matcher must ignore the folder path AND punctuation so the file
    is found anywhere under the downloads tree."""

    def test_punctuation_stripped_key_collapses_variants(self):
        assert dcs._punctuation_stripped_key(
            "Par‐delà le mur des siècles (instrumental).flac"
        ) == dcs._punctuation_stripped_key("Par-dela le mur des siecles instrumental.flac")
        assert dcs._punctuation_stripped_key(
            "Le Cimetière marin (instrumental).flac"
        ) == dcs._punctuation_stripped_key("Le cimetiere marin instrumental.flac")

    def test_wait_for_transfer_file_finds_hyphen_drift(self, monkeypatch):
        """A file whose on-disk basename differs ONLY by hyphen/paren/accents
        from the queue's found_filename must be found by the walk."""
        root = tempfile.mkdtemp()
        try:
            deep = os.path.join(root, "music", "Aephanemer", "2025 - Utopie")
            os.makedirs(deep, exist_ok=True)
            target = os.path.join(deep, "Aephanemer - Utopie - 04 Par-dela le mur des siecles.flac")
            open(target, "w").close()

            monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: root)
            # Queue's found_filename uses U+2010 hyphens + accents + a
            # different folder — every exact key fails; punctuation key wins.
            result = dcs._wait_for_transfer_file(
                "music/Aephanemer/[2025] Utopie/Aephanemer - Utopie - 04 Par‐delà le mur des siècles.flac",
                "",
            )
            assert result == target
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_download_index_punct_key_finds_file_anywhere(self):
        """_build_download_index keys files by punctuation-stripped basename,
        so a queue found_filename that differs only in hyphen/paren/accents
        resolves to the on-disk file regardless of its folder."""
        fs_files = [
            {"full_path": "/downloads/Aephanemer/2025 - Utopie/Aephanemer - Utopie - 04 Par-dela le mur des siecles.flac",
             "rel_path": "Aephanemer/2025 - Utopie/Aephanemer - Utopie - 04 Par-dela le mur des siecles.flac"},
        ]
        index = dcs._build_download_index(fs_files)
        punct_key = dcs._punctuation_stripped_key(
            "Aephanemer - Utopie - 04 Par‐delà le mur des siècles.flac"
        )
        hits = index["by_punct"].get(punct_key, [])
        assert len(hits) == 1
        assert hits[0]["full_path"].endswith("Par-dela le mur des siecles.flac")

    def test_fuzzy_candidates_punct_fallback(self):
        """_fuzzy_candidates must surface a basename that matches the queue
        title after punctuation stripping, even when no token index hits."""
        fs_files = [
            {"full_path": "/downloads/x/Aephanemer - Utopie - 15 Contrepoint instrumental.flac",
             "rel_path": "x/Aephanemer - Utopie - 15 Contrepoint instrumental.flac"},
        ]
        index = dcs._build_download_index(fs_files)
        item = {"title": "Contrepoint (instrumental)", "artist": "Aephanemer"}
        candidates = dcs._fuzzy_candidates(index, item, fs_files)
        assert any(c["full_path"].endswith("Contrepoint instrumental.flac") for c in candidates)


class TestDeepFileSearch:
    def test_nested_music_artist_album_file_is_found_with_path_drift(self, monkeypatch):
        """A file at ``<root>/music/Artist/Album/01 - Track.flac`` must be
        found by basename even when the queue's ``found_filename`` uses a
        DIFFERENT album folder than the peer's on-disk layout (e.g. remote
        ``[2025] Utopie`` vs on-disk ``2025 - Utopie``).  The direct
        remote-join candidate misses; only the DEEP basename walk (8 levels,
        previously 3) reaches the file."""
        root = tempfile.mkdtemp()
        try:
            deep = os.path.join(root, "music", "Aephanemer", "2025 - Utopie")
            os.makedirs(deep, exist_ok=True)
            target = os.path.join(deep, "Aephanemer - Utopie - 08 - Utopie (Partie I).flac")
            open(target, "w").close()

            monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: root)
            # The remote path uses a DIFFERENT album folder than on disk —
            # the old 3-level walk + direct join both miss this.
            result = dcs._wait_for_transfer_file(
                "music/Aephanemer/[2025] Utopie/Aephanemer - Utopie - 08 - Utopie (Partie I).flac",
                "",
            )
            assert result == target
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_sibling_torrents_root_is_searched(self, monkeypatch, tmp_path):
        """A file saved under a SIBLING ``torrents`` dir (slskd's complete
        directory differs from the app's downloads dir) must be found."""
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        torrents = tmp_path / "torrents"
        band = torrents / "Ignea - (ex - Parallax)" / "2026 - Darkness (Single)"
        band.mkdir(parents=True)
        target = band / "02. Dreams of Lands Unseen.mp3"
        target.write_bytes(b"x")

        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: str(downloads))
        result = dcs._wait_for_transfer_file(
            "Ignea - (ex - Parallax)/2026 - Darkness (Single)/02. Dreams of Lands Unseen.mp3",
            "",
        )
        assert result == str(target)


class TestSucceededButNotFoundGrace:
    def teardown_method(self):
        from services.downloads import download_pipeline_service as dps
        dps._blocked_peers.clear()

    def test_fresh_item_keeps_downloading(self, monkeypatch):
        """A succeeded transfer whose file has not appeared within the grace
        window must NOT be cancelled/requeued — returns False (leave
        'downloading'), no mark_failed, no cancel."""
        slskd = _FakeSlskd()
        marked = []
        monkeypatch.setattr(
            "db.repositories.queue.mark_failed",
            lambda qid, reason: marked.append((qid, reason)),
        )
        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: "/downloads")

        item = {"id": 10, "found_filename": "music/Artist/Album/01 - Track.flac", "updated_at": dcs._db_now_naive()}
        transfer = {
            "id": "t1",
            "username": "peer",
            "filename": "music/Artist/Album/01 - Track.flac",
            "localFilePath": "",
            "state": "Completed, Succeeded",
        }

        result = dcs._reconcile_transfer_state(item, slskd, active=[transfer], now=dcs._db_now_naive())
        assert result is False
        assert marked == []
        assert slskd.cancelled == []

    def test_stale_item_fails_after_grace(self, monkeypatch):
        """After the grace period the succeeded-but-unfound transfer is
        finally failed, cancelling the transfer + blocking the peer."""
        from datetime import timedelta
        from services.downloads import download_pipeline_service as dps

        slskd = _FakeSlskd()
        marked = []
        monkeypatch.setattr(
            "db.repositories.queue.mark_failed",
            lambda qid, reason: marked.append((qid, reason)),
        )
        monkeypatch.setattr(dcs, "_monitored_downloads_dir", lambda: "/downloads")

        now = dcs._db_now_naive()
        past = now - timedelta(minutes=20)
        item = {
            "id": 10,
            "found_filename": "music/Artist/Album/01 - Track.flac",
            "updated_at": past.strftime("%Y-%m-%d %H:%M:%S"),
        }
        transfer = {
            "id": "t1",
            "username": "peer",
            "filename": "music/Artist/Album/01 - Track.flac",
            "localFilePath": "",
            "state": "Completed, Succeeded",
        }

        result = dcs._reconcile_transfer_state(item, slskd, active=[transfer], now=now)
        assert result is True
        assert marked and "local file not found" in marked[0][1]
        assert slskd.cancelled == [("peer", "t1", True)]
        assert dps._is_peer_blocked("peer", "music/Artist/Album/01 - Track.flac") is True


class TestFreshItemReFetch:
    def test_refresh_returns_none_when_no_longer_downloading(self, monkeypatch):
        """An item that was requeued (no longer 'downloading') must yield
        None so the completion cycle skips it instead of double-failing."""
        from db.engine import db_session
        from sqlalchemy import text as sql_text

        test_id = 987654
        with db_session() as session:
            session.execute(sql_text("""
                CREATE TABLE IF NOT EXISTS download_queue (
                    id INTEGER PRIMARY KEY,
                    status TEXT,
                    found_filename TEXT,
                    artist TEXT,
                    title TEXT
                )
            """))
            session.execute(sql_text("DELETE FROM download_queue WHERE id = :qid"), {"qid": test_id})
            session.execute(sql_text("""
                INSERT INTO download_queue (id, status, found_filename, artist, title)
                VALUES (:qid, 'queued', 'music/old.flac', 'A', 'T')
            """), {"qid": test_id})
            session.commit()

        assert dcs._refresh_downloading_item(test_id) is None

        with db_session() as session:
            session.execute(sql_text("UPDATE download_queue SET status = 'downloading' WHERE id = :qid"), {"qid": test_id})
            session.commit()

        fresh = dcs._refresh_downloading_item(test_id)
        assert fresh is not None
        assert fresh["status"] == "downloading"
        assert fresh["found_filename"] == "music/old.flac"
