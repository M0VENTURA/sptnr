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


class TestTrackStageAnchorsAlbumToFolder:
    """The per-track MB album rewrite must never split a folder across releases.

    When a track's MB resolution returns a SIBLING EDITION of the folder's
    album ("OPVS NOIR Vol. 3 (Instrumental)" for the folder "OPVS NOIR
    Vol. 3"), the folder anchor must win: the MB title is only adopted when it
    is the SAME release as the folder, and an empty album column is filled
    with the folder name so every track of the folder groups together.
    """

    def _resolve(self, monkeypatch, *, album, file_path, mb_album):
        import types

        import services.popularity.stages.track_stage as ts

        artist = "Lord of the Lost"
        title = "My Funeral"
        batch = {
            f"{artist.lower()}::{title.lower()}": {
                "recording_mbid": "rec-0002",
                "confidence": 0.75,
                "title": title,
                "artist": artist,
                "album": mb_album,
            }
        }
        fake_mb = types.SimpleNamespace(
            get_composers_for_recording=lambda mbid: [],
            get_suggested_mbid=lambda *a, **k: ("rec-0002", 0.75),
            lookup_recording_metadata=lambda *a, **k: batch[
                f"{artist.lower()}::{title.lower()}"
            ],
        )
        monkeypatch.setattr(ts, "get_shared_mb_service", lambda: fake_mb)
        result = ts._resolve_track_mb_metadata(
            track_id="t1",
            track={
                "id": "t1",
                "artist": artist,
                "album": album,
                "title": title,
                "file_path": file_path,
            },
            track_title=title,
            track_artist=artist,
            frozen_track=False,
            force_meta=True,
            options={"mb_batch_metadata": batch},
            batch_artist=artist,
            batch_title=title,
        )
        payload = result["payload"]
        return payload.get("album", album)

    def test_empty_album_anchors_to_folder_not_sibling_edition(self, monkeypatch):
        # Track's album column is empty (fresh import); MB resolves the track
        # to the "(Instrumental)" sibling edition.  The album must be the
        # FOLDER name, never the sibling — otherwise one folder splits into
        # several albums on the first metadata scan.
        album = self._resolve(
            monkeypatch,
            album="",
            file_path="/music/Lord of the Lost/OPVS NOIR Vol. 3/02.flac",
            mb_album="OPVS NOIR Vol. 3 (Instrumental)",
        )
        assert album == "OPVS NOIR Vol. 3"

    def test_strong_folder_match_adopts_mb_title(self, monkeypatch):
        # MB returns the SAME album as the folder (case drift only) — the
        # canonical MB title is adopted to normalise the value.
        album = self._resolve(
            monkeypatch,
            album="",
            file_path="/music/Arion/Last Of Us/02.flac",
            mb_album="Last of Us",
        )
        assert album == "Last of Us"

    def test_artist_album_folder_keeps_clean_existing_album(self, monkeypatch):
        # "Artist - Album" folder with a clean album column ("Nightmare") must
        # NOT be rewritten to a per-track MB sibling edition.
        album = self._resolve(
            monkeypatch,
            album="Nightmare",
            file_path="/music/Avenged Sevenfold - Nightmare/02.flac",
            mb_album="Nightmare (Deluxe Edition)",
        )
        assert album == "Nightmare"

    def test_strong_folder_match_repairs_split_album(self, monkeypatch):
        # A track whose column was previously split to a sibling edition is
        # repaired back to the folder's album when MB returns the folder's
        # own release strongly.
        album = self._resolve(
            monkeypatch,
            album="OPVS NOIR Vol. 3 (Instrumental)",
            file_path="/music/Lord of the Lost/OPVS NOIR Vol. 3/02.flac",
            mb_album="OPVS NOIR Vol. 3",
        )
        assert album == "OPVS NOIR Vol. 3"


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

    def test_weak_sibling_folder_match_is_not_chosen_as_canonical(self):
        # Every batch entry is a sibling EDITION of the folder ("Deluxe",
        # "Instrumental"): neither matches the folder strongly enough to be
        # the canonical album.  The collapse must NOT pick one of them as the
        # folder anchor — the folder's own name is authoritative.
        from services.popularity.scan_stage_runner import _collapse_album_mb_batch

        batch = {
            "lord of the lost::kill the lights": {
                "recording_mbid": "rec-1",
                "title": "Kill The Lights",
                "artist": "Lord of the Lost",
                "album": "OPVS NOIR Vol. 3 (Deluxe Edition)",
            },
            "lord of the lost::my funeral": {
                "recording_mbid": "rec-2",
                "title": "My Funeral",
                "artist": "Lord of the Lost",
                "album": "OPVS NOIR Vol. 3 (Instrumental)",
            },
        }
        contexts = self._contexts("OPVS NOIR Vol. 3")
        _collapse_album_mb_batch(batch, contexts, "OPVS NOIR Vol. 3")
        # The canonical is still picked by frequency, but the folder anchor
        # itself must not be a weak sibling (best folder match < 0.85 → the
        # strong-anchor branch was skipped, falling back to most-frequent).
        assert batch["lord of the lost::my funeral"]["album"] in {
            "OPVS NOIR Vol. 3 (Deluxe Edition)",
            "OPVS NOIR Vol. 3 (Instrumental)",
        }
