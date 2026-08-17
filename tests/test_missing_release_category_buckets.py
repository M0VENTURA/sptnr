"""Tests for missing-release category bucketing (secondary-types parsing).

All missing releases previously landed under "Albums" because the MusicBrainz
search/browse APIs can return ``secondary-types`` as a comma-joined STRING
("Live,Compilation") — iterating a string character-by-character never matched
the list membership checks, so every Live/EP/Compilation fell through to
"Album".  The fix normalises the field to a list first.
"""

from __future__ import annotations

from services.metadata.artist_scan_service import _categorize_release, _build_missing_release_items
from services.popularity.release_cache_service import _derive_musicbrainz_category


def _make_rg(primary, secondary=None, title="X"):
    return {
        "title": title,
        "primary-type": primary,
        "primary_type": primary,
        "secondary-types": secondary or [],
        "secondary_types": secondary or [],
        "first-release-date": "2020-01-01",
        "first_release_date": "2020-01-01",
    }


class TestCategorizeReleaseSecondaryTypes:
    def test_string_form_secondary_types(self):
        # The search API returns the comma-joined string — must parse as a list.
        rg = _make_rg("Album", "Live")
        assert _categorize_release(rg) == "Live Album"

        rg = _make_rg("Album", "Compilation")
        assert _categorize_release(rg) == "Compilation"

        rg = _make_rg("Album", "Live,Compilation")
        assert _categorize_release(rg) in ("Live Album", "Compilation")

        rg = _make_rg("Album", "Remix")
        assert _categorize_release(rg) == "Remix"

    def test_list_form_secondary_types(self):
        rg = _make_rg("Album", ["live"])
        assert _categorize_release(rg) == "Live Album"
        rg = _make_rg("Album", ["compilation"])
        assert _categorize_release(rg) == "Compilation"

    def test_plain_album_stays_album(self):
        assert _categorize_release(_make_rg("Album")) == "Album"

    def test_ep_and_single_primary_types(self):
        assert _categorize_release(_make_rg("EP")) == "EP"
        assert _categorize_release(_make_rg("Single")) == "Single"

    def test_secondary_single_and_ep_honoured(self):
        # MB tags short-form releases as primary-type album + secondary single/ep.
        assert _categorize_release(_make_rg("Album", "Single")) == "Single"
        assert _categorize_release(_make_rg("Album", "EP")) == "EP"

    def test_derive_musicbrainz_category_string_form(self):
        rg = _make_rg("Album", "Live")
        assert _derive_musicbrainz_category(rg) == "Live Album"
        rg = _make_rg("Album", "Compilation")
        assert _derive_musicbrainz_category(rg) == "Compilation"


class TestBuildMissingReleaseItemsBuckets:
    def test_missing_items_land_in_proper_categories(self):
        release_groups = [
            _make_rg("Album", title="Plain Album"),
            _make_rg("Album", "Live", title="Live At Wembley"),
            _make_rg("Album", "Remix", title="The Remixes"),
            _make_rg("Album", "Compilation", title="Greatest Hits"),
            _make_rg("EP", title="The EP"),
            _make_rg("Single", title="Hit Single"),
        ]
        missing = _build_missing_release_items(release_groups, existing_norm=set())
        by_category = {item["category"]: item["title"] for item in missing}
        assert by_category["Album"] == "Plain Album"
        assert by_category["Live Album"] == "Live At Wembley"
        assert by_category["Remix"] == "The Remixes"
        assert by_category["Compilation"] == "Greatest Hits"
        assert by_category["EP"] == "The EP"
        assert by_category["Single"] == "Hit Single"


class TestArtistDetailJsCategoryMapping:
    """The artist page JS maps every category to the matching section id.
    The legacy ``studio-albums`` key was stale — plain albums must map to the
    actual ``albums`` section or JS-injected missing albums are dropped."""

    def test_category_to_section_uses_albums_not_stale_studio_albums(self):
        import os

        js_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "js", "artist_detail.js",
        )
        with open(js_path, encoding="utf-8") as f:
            js = f.read()

        # The mapping must route plain albums to the albums section.
        assert "album: 'albums'" in js
        # The stale key is gone from both the mapping and the section loop.
        assert "studio-albums" not in js
        # Every other category maps to its section.
        for fragment in (
            "live_album: 'live-albums'",
            "remix_album: 'remix-albums'",
            "ep: 'eps'",
            "single: 'singles'",
            "compilation: 'compilations'",
        ):
            assert fragment in js

        # The section loop that un-hides populated categories is consistent.
        assert "['albums', 'live-albums', 'remix-albums', 'eps', 'singles', 'compilations']" in js

