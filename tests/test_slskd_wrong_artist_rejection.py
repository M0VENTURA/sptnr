"""Regression tests: Soulseek matching must not grab wrong-artist tracks.

Reproduces the Orville Peck miss: the queue item "Orville Peck - The Fall"
was matched against a completely different band's file —

    The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac

Two weak gates combined to accept it:

1. The artist-evidence gate matched the token "orville" anywhere in the
   remote path — but that token came from the TRACK TITLE ("A Tribute to
   Orville Wilcox"), not from the artist.  A lone shared first name is not
   evidence the target artist is present.
2. The title-substring fallback ("The Fall" ⊂ "The Fall of Troy") awarded
   partial title credit even though the artist is a different band, and the
   score reached the 30-point acceptance threshold.

Fix: the artist must be evidenced in the ARTIST SEGMENT of the candidate
(parsed artist field, or the path with the track-title removed) — either by
parsed-artist similarity, the full artist phrase as a substring, or at least
two significant artist words appearing together.  A single shared first
name in the track title no longer counts.
"""

from __future__ import annotations

from services.downloads.download_pipeline_service import _score_result


def _candidate(filename: str, **overrides):
    """Build a slskd result dict with sensible defaults."""
    result = {
        "filename": filename,
        "bitrate": 320,
        "has_free_upload_slot": True,
        "queue_length": 0,
        "upload_speed": 2_000_000,
    }
    result.update(overrides)
    return result


class TestWrongArtistRejection:
    """A different band whose track title shares a first name must be rejected."""

    def test_orville_peck_not_matched_to_fall_of_troy(self):
        """'Orville Peck - The Fall' must NOT match The Fall of Troy's file.

        The candidate path contains the token 'orville' only inside the track
        title ('A Tribute to Orville Wilcox') — no artist evidence.
        """
        candidate = _candidate(
            "music\\The Fall of Troy (post‐hardcore band)\\[2020] Mukiltearth\\"
            "The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac"
        )
        assert _score_result(
            candidate, "Orville Peck", "The Fall", expected_year=None,
        ) == 0.0

    def test_title_substring_without_artist_evidence_is_rejected(self):
        """A same-worded title by another band ('The Fall' ⊂ 'The Fall of Troy')"""
        candidate = _candidate(
            "The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac"
        )
        assert _score_result(
            candidate, "Orville Peck", "The Fall",
        ) == 0.0

    def test_single_shared_first_name_is_not_artist_evidence(self):
        """A lone first-name token shared with a song title is not evidence."""
        candidate = _candidate("Other Artist - Some Song ft. Orville Whatever.flac")
        assert _score_result(
            candidate, "Orville Peck", "The Fall",
        ) == 0.0


class TestLegitimateMatchesStillPass:
    """Real matches must keep scoring above the acceptance threshold."""

    def test_artist_album_title_parse_matches(self):
        candidate = _candidate(
            "Orville Peck - Stampede - 02 The Fall.flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0

    def test_artist_folder_evidence_matches(self):
        """Artist name in a parent folder (not the filename) is evidence."""
        candidate = _candidate(
            "music\\Orville Peck\\[2024] Stampede\\Orville Peck - The Fall.flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0

    def test_full_artist_phrase_anywhere_matches(self):
        candidate = _candidate(
            "Orville Peck - The Fall (Live).flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0


class TestMultiWordArtistEvidence:
    """Two significant artist words together count as evidence."""

    def test_two_word_artist_in_scope_is_evidenced(self):
        candidate = _candidate(
            "Various - The Pretty Reckless - Heaven Knows.flac"
        )
        # 'pretty' + 'reckless' together in the artist-scope tokens.
        assert _score_result(candidate, "The Pretty Reckless", "Heaven Knows") >= 30.0

    def test_one_of_two_artist_words_is_not_evidence(self):
        """Only one of 'pretty'/'reckless' appearing is not evidence."""
        candidate = _candidate(
            "A Pretty Song - Heaven Knows.flac"
        )
        assert _score_result(candidate, "The Pretty Reckless", "Heaven Knows") == 0.0


class TestAlbumArtistsAndEdgeCases:
    """Album-artist credits and generic-artist skips."""

    def test_album_artist_in_path_is_evidenced(self):
        candidate = _candidate(
            "music\\Various Artists\\[2024] Stampede\\Orville Peck - The Fall.flac"
        )
        assert _score_result(candidate, "Orville Peck", "The Fall") >= 30.0

    def test_unknown_artist_skips_gate(self):
        """'Unknown' artist skips the gate (title-based fallback allowed).

        With no artist to verify, the title substring may apply — the gate
        only protects KNOWN artists from wrong-artist grabs.
        """
        candidate = _candidate(
            "The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac"
        )
        score = _score_result(candidate, "Unknown", "The Fall")
        # Gate skipped → partial title substring credit applies (> 0).
        assert score > 0.0

    def test_generic_various_artist_skips_gate(self):
        candidate = _candidate(
            "Various Artists - The Fall - Compilation.flac"
        )
        assert _score_result(candidate, "Various Artists", "The Fall") >= 30.0


class TestWrongAlbumRejection:
    """A file from the WRONG ALBUM must be rejected even when the artist
    matches — searching "Lament for the Hollow" (from *Obscured Horizons*)
    must never download "07 - Yesterday's Fire" from a different release."""

    def test_same_artist_different_album_rejected(self):
        candidate = _candidate(
            "The Eternal - When The Circle Of Light Begins To Fade - 07 - Yesterday's Fire.flac"
        )
        assert _score_result(
            candidate,
            "The Eternal", "Lament for the Hollow",
            expected_album="Obscured Horizons",
        ) == 0.0

    def test_folder_album_mismatch_rejected(self):
        """Album in the parent folder (basename has only a track number)."""
        candidate = _candidate(
            "music/The Eternal [AUS]/2013 - When The Circle Of Light Begins To Fade/07 - Yesterday's Fire.flac"
        )
        assert _score_result(
            candidate,
            "The Eternal", "Lament for the Hollow",
            expected_album="Obscured Horizons",
        ) == 0.0

    def test_correct_album_still_passes(self):
        candidate = _candidate(
            "The Eternal - Obscured Horizons - 01 - Lament for the Hollow.flac"
        )
        assert _score_result(
            candidate,
            "The Eternal", "Lament for the Hollow",
            expected_album="Obscured Horizons",
        ) >= 30.0

    def test_track_number_only_basename_parses_title(self):
        """'07 - Yesterday's Fire.flac' must parse '07' as the track number,
        NOT the artist (previously the artist gate gave folder-artist credit
        and let wrong files through)."""
        from services.downloads.download_pipeline_service import _parse_filename_parts

        parts = _parse_filename_parts("07 - Yesterday's Fire.flac")
        assert parts["artist"] is None
        assert parts["title"] == "Yesterday's Fire"
        assert parts["has_track_number"] is True
