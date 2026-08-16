"""Regression tests: a metadata update must never split one album into two.

The per-track MusicBrainz lookup resolves each track to whichever release the
recording is listed under first.  For multi-edition albums ("OPVS NOIR
Vol. 3" vs "OPVS NOIR Vol. 3 (Instrumental)") different tracks of the SAME
folder can therefore resolve to different release titles.  If every track
writes its own resolved name to the ``album`` column, one folder silently
becomes several albums on every metadata scan.

Guards:
- ``track_stage`` never rewrites an album column that already matches the
  folder (the folder is authoritative).
- ``scan_stage_runner`` collapses an album's whole MB batch onto one
  folder-anchored album name before any track is processed.
"""

from __future__ import annotations


def _default_single_result():
    return {
        "is_single": False,
        "confidence": "low",
        "confidence_score": 0.0,
        "single_status": "none",
        "sources": [],
        "reasons": [],
        "decision": {},
    }


class TestTrackStageDoesNotSplitAlbums:
    """The per-track MB album rewrite is skipped when the column matches the folder."""

    def _run(self, monkeypatch, *, title, album, file_path, mb_album):
        import types

        import services.popularity.stages.track_stage as ts

        artist = "Lord of the Lost"
        captured: dict = {}

        batch = {
            f"{artist.lower()}::{title.lower()}": {
                "recording_mbid": "rec-0001",
                "confidence": 0.9,
                "title": title,
                "artist": artist,
                "album": mb_album,
            }
        }

        monkeypatch.setattr(ts, "ListenBrainzClient", lambda *a, **k: None)
        monkeypatch.setattr(ts, "LastFmClient", lambda *a, **k: None)
        monkeypatch.setattr(ts, "get_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_search_aggregated_lastfm_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_aggregated_listenbrainz_popularity", lambda *a, **k: {})
        monkeypatch.setattr(ts, "get_shared_mb_client", lambda: None)
        monkeypatch.setattr(
            ts, "detect_single_for_track", lambda **kw: _default_single_result()
        )

        def _fake_mb(*a, **k):
            return types.SimpleNamespace(
                get_composers_for_recording=lambda mbid: [],
                get_suggested_mbid=lambda *a, **k: ("rec-0001", 0.9),
                lookup_recording_metadata=lambda *a, **k: batch[
                    f"{artist.lower()}::{title.lower()}"
                ],
            )

        monkeypatch.setattr(ts, "get_shared_mb_service", _fake_mb)
        monkeypatch.setattr(
            ts,
            "insert_or_update_track",
            lambda track_id, effective_track: captured.update(effective_track),
        )

        track = {
            "id": "t1",
            "artist": artist,
            "album": album,
            "title": title,
            "file_path": file_path,
            "final_score": 0.0,
            "lastfm_listeners": 0,
            "listenbrainz_listens": 0,
            "single_detection_last_updated": None,
        }
        album_context = {
            "album": album,
            "artist": artist,
            "tracks": [track],
            "is_live_album": False,
        }
        ts.process_track(
            track=track,
            track_context={"artist": artist, "album": album, "lastfm_title": title},
            album_context=album_context,
            album_result={"detected_album_type": "album", "is_heterogeneous": False},
            options={"mb_batch_metadata": batch},
        )
        return captured

    def test_mb_instrumental_release_does_not_split_consistent_album(self, monkeypatch):
        # Both tracks live in the "OPVS NOIR Vol. 3" folder and the album
        # column already matches it.  Even when MB resolves one track to the
        # sibling "(Instrumental)" release, the album must NOT be rewritten.
        captured = self._run(
            monkeypatch,
            title="My Funeral",
            album="OPVS NOIR Vol. 3",
            file_path="/music/Lord of the Lost/OPVS NOIR Vol. 3/My Funeral.flac",
            mb_album="OPVS NOIR Vol. 3 (Instrumental)",
        )
        assert captured.get("album") == "OPVS NOIR Vol. 3"

    def test_matching_mb_release_keeps_consistent_album(self, monkeypatch):
        captured = self._run(
            monkeypatch,
            title="Kill The Lights",
            album="OPVS NOIR Vol. 3",
            file_path="/music/Lord of the Lost/OPVS NOIR Vol. 3/Kill The Lights.flac",
            mb_album="OPVS NOIR Vol. 3",
        )
        assert captured.get("album") == "OPVS NOIR Vol. 3"

    def test_mb_lookup_still_fixes_folder_mismatch(self, monkeypatch):
        # A folder whose album column was corrupted (a previous bad MB match)
        # is still repaired back to the folder-anchored name.
        captured = self._run(
            monkeypatch,
            title="My Funeral",
            album="OPVS NOIR Vol. 3 (Instrumental)",
            file_path="/music/Lord of the Lost/OPVS NOIR Vol. 3/My Funeral.flac",
            mb_album="OPVS NOIR Vol. 3",
        )
        assert captured.get("album") == "OPVS NOIR Vol. 3"


class TestCollapseAlbumMbBatch:
    """The album-level MB batch is collapsed onto one folder-anchored name."""

    def _contexts(self, folder):
        return [
            {
                "title": "Kill The Lights",
                "artist": "Lord of the Lost",
                "track": {
                    "title": "Kill The Lights",
                    "file_path": f"/music/Lord of the Lost/{folder}/01.flac",
                },
            },
            {
                "title": "My Funeral",
                "artist": "Lord of the Lost",
                "track": {
                    "title": "My Funeral",
                    "file_path": f"/music/Lord of the Lost/{folder}/02.flac",
                },
            },
        ]

    def _batch(self):
        return {
            "lord of the lost::kill the lights": {
                "recording_mbid": "rec-1",
                "title": "Kill The Lights",
                "artist": "Lord of the Lost",
                "album": "OPVS NOIR Vol. 3",
            },
            "lord of the lost::my funeral": {
                "recording_mbid": "rec-2",
                "title": "My Funeral",
                "artist": "Lord of the Lost",
                "album": "OPVS NOIR Vol. 3 (Instrumental)",
            },
        }

    def test_all_entries_rewritten_to_folder_anchor(self):
        from services.popularity.scan_stage_runner import _collapse_album_mb_batch

        batch = self._batch()
        _collapse_album_mb_batch(
            batch,
            self._contexts("OPVS NOIR Vol. 3"),
            "OPVS NOIR Vol. 3",
        )
        albums = {meta["album"] for meta in batch.values()}
        assert albums == {"OPVS NOIR Vol. 3"}

    def test_folder_match_wins_over_majority_sibling_edition(self):
        from services.popularity.scan_stage_runner import _collapse_album_mb_batch

        batch = self._batch()
        # A majority of MB hits pointing at the "(Instrumental)" sibling
        # edition must NOT outrank the entry that matches the folder — the
        # folder is authoritative, so the whole album collapses onto it.
        batch["lord of the lost::kill the lights"]["album"] = (
            "OPVS NOIR Vol. 3 (Instrumental)"
        )
        batch["lord of the lost::my funeral"]["album"] = (
            "OPVS NOIR Vol. 3 (Instrumental)"
        )
        batch["lord of the lost::the shadows within"] = {
            "recording_mbid": "rec-3",
            "title": "The Shadows Within",
            "artist": "Lord of the Lost",
            "album": "OPVS NOIR Vol. 3",
        }
        contexts = self._contexts("OPVS NOIR Vol. 3") + [
            {
                "title": "The Shadows Within",
                "artist": "Lord of the Lost",
                "track": {
                    "title": "The Shadows Within",
                    "file_path": "/music/Lord of the Lost/OPVS NOIR Vol. 3/03.flac",
                },
            }
        ]
        _collapse_album_mb_batch(batch, contexts, "OPVS NOIR Vol. 3")
        albums = {meta["album"] for meta in batch.values()}
        assert albums == {"OPVS NOIR Vol. 3"}
