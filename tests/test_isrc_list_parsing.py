"""Tests for the ISRC list-bracket parsing fix.

The bug: a raw list value (Navidrome ``tags`` map, MusicBrainz ``isrcs``
array) was converted with ``str()`` — producing the literal
``"['NLA321400382/NLA321400448']"`` — which no 12-char regex could match,
so the bracketed string leaked downstream into the ISRC lookups
(``resolve_isrc_recording`` / ListenBrainz by-recording) as a junk key and
the scan fell back to the slow album-tracklist LB match.

Fix: ``normalize_isrc`` now unpacks list/tuple/set values and returns the
first valid 12-char code; ``resolve_isrc_recording`` refuses bracketed
input before calling the API.
"""

from __future__ import annotations

import pytest

from helpers.normalization_service import normalize_isrc


class TestNormalizeIsrcListHandling:
    def test_plain_code_unchanged(self):
        assert normalize_isrc("NLA321292284") == "NLA321292284"

    def test_clean_slash_code(self):
        assert normalize_isrc("NLA321400382/NLA321400448") == "NLA321400382"

    def test_raw_list_value(self):
        # The exact regression: a LIST (from Navidrome tags / MB isrcs)
        # must NOT become "['NLA321400382/NLA321400448']".
        assert normalize_isrc(["NLA321400382/NLA321400448"]) == "NLA321400382"

    def test_raw_single_item_list(self):
        assert normalize_isrc(["NLA320119564"]) == "NLA320119564"

    def test_multiple_list_items_returns_first_valid(self):
        assert normalize_isrc(["USRC17607839", "USRC17607840"]) == "USRC17607839"

    def test_tuple_value(self):
        assert normalize_isrc(("NLA321292284",)) == "NLA321292284"

    def test_empty_list_returns_empty(self):
        assert normalize_isrc([]) == ""
        assert normalize_isrc(()) == ""

    def test_none_returns_empty(self):
        assert normalize_isrc(None) == ""

    def test_lowercase_is_uppercased(self):
        assert normalize_isrc("nla321292284") == "NLA321292284"


class TestResolveIsrcBracketedGuard:
    def test_bracketed_string_never_reaches_api(self, monkeypatch):
        """A bracketed tag-list string is normalised before the API call."""
        from services.popularity import popularity_sources as ps

        calls: list[str] = []

        class _FakeMB:
            def lookup_by_isrc(self, isrc: str, inc: str = ""):
                calls.append(isrc)
                return [{"id": "rec-1", "title": "Custer", "artist-credit": [{"name": "Slipknot"}]}]

        fake = _FakeMB()
        result = ps.resolve_isrc_recording(
            "['NLA321400382/NLA321400448']",
            mb_client=fake,
            title="Custer",
            artist="Slipknot",
        )
        # The API received the BARE code, not the bracketed string.
        assert calls == ["NLA321400382"]
        assert result is not None
        assert result["recording_mbid"] == "rec-1"

    def test_unparseable_bracketed_returns_none(self, monkeypatch):
        from services.popularity import popularity_sources as ps

        class _NeverCalled:
            def lookup_by_isrc(self, isrc: str, inc: str = ""):
                raise AssertionError("should not be called with junk")

        result = ps.resolve_isrc_recording(
            "[junk]",
            mb_client=_NeverCalled(),
            title="X",
            artist="Y",
        )
        assert result is None
