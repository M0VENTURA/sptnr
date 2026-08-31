"""Tests for missing-release population from MusicBrainz.

Verifies ``_build_missing_release_items`` includes ALL release types the
artist page displays — Albums, Live Albums, Remix Albums, Compilations, EPs
and Singles — and only excludes releases already in the library.
"""

from __future__ import annotations

import pytest


def _make_rg(title, primary, secondary=None, release_date="2020-01-01", rg_id=None):
    return {
        "id": rg_id or f"id-{title.lower().replace(' ', '-')}",
        "title": title,
        "primary-type": primary,
        "primary_type": primary,
        "secondary-types": secondary or [],
        "secondary_types": secondary or [],
        "first-release-date": release_date,
        "first_release_date": release_date,
        "cover-art-archive": {"artwork": True, "count": 1},
    }


def test_missing_releases_include_all_categories():
    """Albums, Live Albums, Remix Albums, Compilations, EPs and Singles are
    all included as missing releases (no category filtering)."""
    from services.metadata.artist_scan_service import _build_missing_release_items

    release_groups = [
        _make_rg("Regular Album", "Album"),
        _make_rg("Live At Wembley", "Album", ["live"]),
        _make_rg("The Remixes", "Album", ["remix"]),
        _make_rg("Greatest Hits", "Album", ["compilation"]),
        _make_rg("The EP", "EP"),
        _make_rg("Hit Single", "Single", release_date="2015-06-01"),
        _make_rg("Old Single", "Single", release_date="2005-01-01"),
    ]

    missing = _build_missing_release_items(release_groups, existing_norm=set())

    by_category = {item["category"]: item["title"] for item in missing}
    assert by_category["Album"] == "Regular Album"
    assert by_category["Live Album"] == "Live At Wembley"
    assert by_category["Remix"] == "The Remixes"
    assert by_category["Compilation"] == "Greatest Hits"
    assert by_category["EP"] == "The EP"
    # All singles are included, not just current-year ones.
    assert by_category["Single"] in ("Hit Single", "Old Single")
    assert len([i for i in missing if i["category"] == "Single"]) == 2


def test_missing_releases_exclude_library_and_non_music_types():
    """Releases already in the library and non album/ep/single types are
    excluded."""
    from services.metadata.artist_scan_service import _build_missing_release_items

    release_groups = [
        _make_rg("Already Owned", "Album"),
        _make_rg("New Album", "Album"),
        _make_rg("Some Broadcast", "Broadcast"),  # non music type → excluded
    ]
    missing = _build_missing_release_items(release_groups, existing_norm={"already owned"})

    titles = [item["title"] for item in missing]
    assert "New Album" in titles
    assert "Already Owned" not in titles
    assert "Some Broadcast" not in titles


def test_missing_releases_singles_current_year_flag():
    """The legacy current-year-only singles flag still works when enabled."""
    from services.metadata.artist_scan_service import _build_missing_release_items

    from datetime import datetime
    now_year = datetime.now().year

    release_groups = [
        _make_rg("This Year Single", "Single", release_date=f"{now_year}-03-01"),
        _make_rg("Old Single", "Single", release_date="2010-03-01"),
    ]
    missing = _build_missing_release_items(
        release_groups,
        existing_norm=set(),
        include_singles_current_year_only=True,
    )
    titles = [item["title"] for item in missing]
    assert "This Year Single" in titles
    assert "Old Single" not in titles


def test_missing_releases_prior_year_singles_not_covered_by_library_track():
    """A prior-year single is included as missing — UNLESS its track is already
    on a library album.  The user's request: singles that aren't matched to any
    track on an album release from prior years should still show up."""
    from services.metadata.artist_scan_service import _build_missing_release_items

    release_groups = [
        # 2018 single whose track is NOT in the library → still missing.
        _make_rg("Queen Dies", "Single", release_date="2018-09-14"),
        # 2018 single whose track IS on a library album → covered, not missing.
        _make_rg("Realms Of Fire", "Single", release_date="2018-04-13"),
        # A plain album for contrast.
        _make_rg("The Realms of Fire and Death", "Album", release_date="2018-11-16"),
    ]
    library_tracks = {"queen dies", "the realms of fire and death", "other track"}

    missing = _build_missing_release_items(
        release_groups,
        existing_norm=set(),
        library_track_titles=library_tracks,
    )

    by_category = {item["title"]: item["category"] for item in missing}
    # "Queen Dies" single is NOT on any library album → stays missing.
    assert by_category["Queen Dies"] == "Single"
    # "Realms Of Fire" single is covered by the album track → excluded.
    assert "Realms Of Fire" not in by_category
    # The album itself is not in the library → still missing.
    assert by_category["The Realms of Fire and Death"] == "Album"
