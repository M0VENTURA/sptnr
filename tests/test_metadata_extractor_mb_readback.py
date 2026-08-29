"""Regression tests: the Navidrome metadata extractor reads MB enrichment
fields back from file tags.

The download-completion import writes ``IS COVER`` / ``ORIGINAL COVER ARTIST``
/ ``MUSICBRAINZ GENRES`` TXXX/Vorbis frames onto the moved file.  A later
Navidrome import must read those fields back into the tracks table so the
enrichment is preserved — previously the extractor never read them, so a
rescan dropped the cover flag / cover artist / MB genres even though they
were sitting in the file tags.
"""

from __future__ import annotations

from services.scanning.metadata_extractor import extract_track_metadata


def _track(tags: dict | None = None) -> dict:
    track = {
        "id": "t1",
        "title": "Contrepoint",
        "artist": "Aephanemer",
        "album": "Utopie",
        "path": "/music/Aephanemer/2025 - Utopie/15 - Contrepoint.flac",
        "duration": 240,
        "trackNumber": 15,
        "discNumber": 1,
        "year": 2025,
        "genres": ["Melodic Death Metal"],
    }
    if tags:
        track["tags"] = tags
    return track


class TestExtractorReadsMbEnrichmentBack:
    def test_reads_is_cover_from_tags(self):
        meta = extract_track_metadata(_track(tags={"IS_COVER": "1"}))
        assert str(meta.get("is_cover") or "").strip() == "1"

    def test_reads_is_cover_from_snake_case_key(self):
        meta = extract_track_metadata(_track(tags={"is_cover": "1"}))
        assert str(meta.get("is_cover") or "").strip() == "1"

    def test_reads_original_cover_artist(self):
        meta = extract_track_metadata(_track(tags={"ORIGINAL_COVER_ARTIST": "Original Artist"}))
        assert meta.get("original_cover_artist") == "Original Artist"

    def test_reads_musicbrainz_genres(self):
        meta = extract_track_metadata(_track(tags={"musicbrainz_genres": "melodic death metal"}))
        assert meta.get("musicbrainz_genres") == "melodic death metal"

    def test_reads_musicbrainz_genres_txxx_desc(self):
        meta = extract_track_metadata(_track(tags={"MUSICBRAINZ GENRES": "melodic death metal"}))
        assert meta.get("musicbrainz_genres") == "melodic death metal"

    def test_reads_work_mbid(self):
        meta = extract_track_metadata(_track(tags={"musicbrainz_workid": "work-123"}))
        assert meta.get("musicbrainz_workid") == "work-123"
