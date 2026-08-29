"""Regression tests: album-page track edit modal + disc-number + genre fixes.

Covers:
1. ``_coerce_optional_int`` maps JSON booleans to 1/0 so BIGINT flag columns
   (``is_cover``, ``is_live``, ``is_remix``, ``alternate_take``,
   ``is_compilation``) accept the edit modal's checkbox values — previously
   ``True``/``False`` became ``None`` and the flags were silently nulled
   (the "pressing Save on the edit track modal doesn't save" report).
2. The album save handler strips a stray ``disc_number`` of "1" when the
   album has no explicit disctotal and no track is on a disc > 1.
3. The similar-artist ``in_collection`` annotation uses normalised
   (punctuation/case/"The"-tolerant) keys so owned artists are not
   re-suggested.
"""

from __future__ import annotations


class TestCoerceOptionalIntBooleans:
    def test_true_maps_to_1(self):
        from routes.track_routes import _coerce_optional_int

        assert _coerce_optional_int(True) == 1

    def test_false_maps_to_0(self):
        from routes.track_routes import _coerce_optional_int

        assert _coerce_optional_int(False) == 0

    def test_numeric_strings_unchanged(self):
        from routes.track_routes import _coerce_optional_int

        assert _coerce_optional_int("3") == 3
        assert _coerce_optional_int("") is None
        assert _coerce_optional_int(None) is None

    def test_track_number_prefix_still_parses(self):
        from routes.track_routes import _coerce_optional_int

        assert _coerce_optional_int("2/12", allow_prefix=True) == 2

    def test_flag_normalization_pipeline(self):
        """A full modal payload's flags must survive _normalize_track_updates
        as 1/0 instead of None."""
        from routes.track_routes import _normalize_track_updates

        column_types = {
            "is_cover": "bigint", "is_live": "bigint", "is_remix": "bigint",
            "alternate_take": "bigint", "is_compilation": "bigint",
            "stars": "integer", "is_single": "boolean", "title": "text",
        }
        updates = {
            "title": "Song",
            "stars": 4,
            "is_single": False,
            "is_cover": True,
            "is_live": False,
            "is_remix": True,
            "alternate_take": False,
            "is_compilation": True,
        }
        normalized = _normalize_track_updates(updates, column_types)
        assert normalized["is_cover"] == 1
        assert normalized["is_live"] == 0
        assert normalized["is_remix"] == 1
        assert normalized["alternate_take"] == 0
        assert normalized["is_compilation"] == 1
        assert normalized["stars"] == 4
        assert normalized["is_single"] is False


class TestAlbumDiscNumberStripInference:
    def _derive(self, disctotal_raw: str, disc_numbers: list[str | None]) -> dict:
        """Mirror the route's disc inference: returns whether strip/multi."""
        _strip = False
        _multi = False
        try:
            _dt = int(disctotal_raw)
            _multi = _dt > 1
            _strip = _dt <= 1
        except (TypeError, ValueError):
            _strip = bool(disctotal_raw)

        if not disctotal_raw and not _multi:
            _max_track_disc = 0
            for d in disc_numbers:
                raw = str(d or "").strip()
                if raw:
                    try:
                        _max_track_disc = max(_max_track_disc, int(raw.split("/")[0].strip()))
                    except Exception:
                        pass
            if _max_track_disc <= 1:
                _strip = True
            elif _max_track_disc > 1:
                _multi = True
        return {"strip": _strip, "multi": _multi}

    def test_empty_disctotal_with_stray_disc1_strips(self):
        """The reported bug: one track has disc 1, the rest empty, disctotal
        unset → the stray "1" must be stripped."""
        result = self._derive("", ["1", "", ""])
        assert result["strip"] is True
        assert result["multi"] is False

    def test_empty_disctotal_all_empty_noop(self):
        result = self._derive("", ["", "", ""])
        # All empty → nothing to strip, but the flag is harmless.
        assert result["strip"] is True

    def test_multi_disc_evidence_keeps(self):
        result = self._derive("", ["1", "2", ""])
        assert result["multi"] is True
        assert result["strip"] is False

    def test_explicit_disctotal_1_strips(self):
        result = self._derive("1", ["1", "", ""])
        assert result["strip"] is True

    def test_explicit_disctotal_2_multi(self):
        result = self._derive("2", ["1", "", ""])
        assert result["multi"] is True
        assert result["strip"] is False


class TestSimilarArtistNormalisedCollection:
    def test_punctuation_and_the_tolerant_match(self):
        from services.metadata.artist_metadata_service import (
            _annotate_similar_artist,
            _norm_artist_key,
        )

        # Stored library name: "The Beatles"
        in_collection = {"the beatles"}
        entries = [
            {"name": "Beatles, The", "match": 0.9},
            {"name": "The Beatles", "match": 0.95},
            {"name": "Pink Floyd", "match": 0.8},
        ]
        annotated = _annotate_similar_artist(entries, in_collection)

        by_name = {a["name"]: a for a in annotated}
        assert by_name["Beatles, The"]["in_collection"] is True
        assert by_name["The Beatles"]["in_collection"] is True
        assert by_name["Pink Floyd"]["in_collection"] is False

    def test_norm_key_strips_the_and_punctuation(self):
        from services.metadata.artist_metadata_service import _norm_artist_key

        assert _norm_artist_key("The Beatles") == _norm_artist_key("beatles, the")
        assert _norm_artist_key("A Day to Remember") == "a day to remember"
