"""Tests for similar-artist recommendation handling.

Covers the two behaviour changes that keep recommendations useful:
- ``in_collection`` annotation so the frontend can tell owned artists apart
- filtering owned artists out of the pre-rendered artist-detail list
"""

from __future__ import annotations

import pytest

from services.metadata.artist_metadata_service import _annotate_similar_artist


class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Minimal stand-in for a SQLAlchemy session around ``execute``."""

    def __init__(self, owned):
        self.owned = {o.lower() for o in owned}
        self.executed = False

    def execute(self, statement, params):
        self.executed = True
        return _FakeRows([(name,) for name in self.owned])


def test_annotate_similar_artist_marks_string_entries():
    owned = {"coldplay", "radiohead"}
    entries = ["Coldplay", "The Killers"]
    result = _annotate_similar_artist(entries, owned)

    assert result == [
        {"name": "Coldplay", "match": 0.0, "in_collection": True},
        {"name": "The Killers", "match": 0.0, "in_collection": False},
    ]


def test_annotate_similar_artist_marks_dict_entries_and_keeps_match():
    owned = {"coldplay"}
    entries = [
        {"name": "Coldplay", "match": 0.93},
        {"name": "Pixies", "match": 0.5},
    ]
    result = _annotate_similar_artist(entries, owned)

    assert result == [
        {"name": "Coldplay", "match": 0.93, "in_collection": True},
        {"name": "Pixies", "match": 0.5, "in_collection": False},
    ]


def test_annotate_similar_artist_skips_blank_names():
    assert _annotate_similar_artist(["", "  ", 123], set()) == []


def test_annotate_is_case_insensitive():
    # ``in_collection`` is expected to already hold lowercased names
    # (as returned by the catalogue lookup).
    result = _annotate_similar_artist(["Coldplay"], {"coldplay"})
    assert result[0]["in_collection"] is True


def test_display_list_filters_owned_artists():
    from routes.ui_routes import _similar_artist_display_list

    session = _FakeSession(owned=["Coldplay", "Radiohead"])
    entries = [
        "Coldplay",
        {"name": "Radiohead", "match": 0.8},
        {"name": "The Killers", "match": 0.4},
        {"name": "  "},
    ]
    result = _similar_artist_display_list(session, entries)

    assert [r["name"] for r in result] == ["The Killers"]
    assert result[0]["match"] == 0.4
    assert session.executed


def test_display_list_empty_input_never_queries():
    from routes.ui_routes import _similar_artist_display_list

    session = _FakeSession(owned=["anything"])
    assert _similar_artist_display_list(session, []) == []
    assert not session.executed
