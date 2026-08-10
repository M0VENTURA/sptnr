"""Regression tests for the Reorganisation Plan (issue #867).

Covers the "Core Data & Scraper Fixes":
- ``_categorize_release`` routes MusicBrainz release-groups tagged with a
  ``single`` / ``ep`` secondary type into the Singles / EPs buckets instead of
  cluttering Albums (e.g. Poppy's *Guardian*, primary=album secondary=single).
- ``ArtistBioService.get_artist_biography`` disambiguates single-word artist
  names ("Poppy") by appending ``(singer)`` / ``(musician)`` to the Wikidata
  search query before falling back to the bare name.
"""

from __future__ import annotations

import pytest


class TestSingleRoutingFromSecondaryTypes:
    """services.metadata.artist_scan_service._categorize_release."""

    def _cat(self, primary, secondary=()):
        from services.metadata.artist_scan_service import _categorize_release

        return _categorize_release(
            {
                "primary-type": primary,
                "primary_type": primary,
                "secondary-types": list(secondary),
                "secondary_types": list(secondary),
            }
        )

    def test_album_with_single_secondary_routes_to_single(self):
        # The *Guardian* case: MusicBrainz marks the release-group
        # primary="album" with a "single" secondary type.
        assert self._cat("album", ["single"]) == "Single"

    def test_album_with_ep_secondary_routes_to_ep(self):
        assert self._cat("album", ["ep"]) == "EP"

    def test_plain_album_stays_album(self):
        assert self._cat("album", []) == "Album"

    def test_plain_single_stays_single(self):
        assert self._cat("single", []) == "Single"

    def test_plain_ep_stays_ep(self):
        assert self._cat("ep", []) == "EP"

    def test_compilation_secondary_wins_for_album(self):
        assert self._cat("album", ["compilation"]) == "Compilation"

    def test_live_secondary_wins_for_album(self):
        assert self._cat("album", ["live"]) == "Live Album"

    def test_remix_secondary_wins_for_album(self):
        assert self._cat("album", ["remix"]) == "Remix"

    def test_non_album_primary_returned_verbatim(self):
        assert self._cat("broadcast", []) == "Album"


class TestSingleWordBioDisambiguation:
    """ArtistBioService appends a musician qualifier for single-word names."""

    def _make_service(self, search_results):
        from services.enrichment.artist_bio_service import ArtistBioService

        captured = []

        class FakeHttp:
            def search_entities(self, query, limit=5):
                captured.append(query)
                return list(search_results.get(query, []))

            def get_entity(self, entity_id):
                return {"sitelinks": {"enwiki": {"title": "Poppy (singer)"}}}

            def get_wikipedia_summary(self, title):
                return "Poppy is an American singer-songwriter."

        service = ArtistBioService(http_client=FakeHttp())
        return service, captured

    def test_single_word_queries_singer_first(self):
        results = {
            "Poppy (singer)": [{"id": "Q1", "label": "Poppy", "description": "American singer"}],
        }
        service, captured = self._make_service(results)
        bio = service.get_artist_biography("Poppy")
        assert bio == "Poppy is an American singer-songwriter."
        assert captured == ["Poppy (singer)"]

    def test_single_word_falls_back_to_musician(self):
        results = {
            "Poppy (musician)": [{"id": "Q2", "label": "Poppy", "description": "British musician"}],
        }
        service, captured = self._make_service(results)
        bio = service.get_artist_biography("Poppy")
        assert bio == "Poppy is an American singer-songwriter."
        assert captured == ["Poppy (singer)", "Poppy (musician)"]

    def test_single_word_falls_back_to_bare_name(self):
        results = {
            "Poppy": [{"id": "Q3", "label": "Poppy", "description": "singer"}],
        }
        service, captured = self._make_service(results)
        bio = service.get_artist_biography("Poppy")
        assert bio == "Poppy is an American singer-songwriter."
        assert captured == ["Poppy (singer)", "Poppy (musician)", "Poppy"]

    def test_multi_word_name_queries_bare_name_only(self):
        results = {
            "The Beatles": [{"id": "Q4", "label": "The Beatles", "description": "English rock band"}],
        }
        service, captured = self._make_service(results)
        bio = service.get_artist_biography("The Beatles")
        assert bio == "Poppy is an American singer-songwriter."
        assert captured == ["The Beatles"]
