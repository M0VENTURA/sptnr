"""Regression tests: edition-annotated tracks must only match edition singles.

Reproduces the Feuerschwanz "Knightclub" scan, where "(Epic Edition)" versions
of songs were flagged as MusicBrainz singles because title normalisation strips
brackets, so "Valhalla (Epic Edition)" collided with the plain "Valhalla"
single release group (only the non-edition single exists on MusicBrainz).

Verified against live MusicBrainz for Feuerschwanz (artist
107fe9d9-903d-4bdc-ba88-d1df3c2fa32c):
- "Das Elfte Gebot (Epic Edition)" IS a real Single release group → must match.
- "Valhalla (Epic Edition)" / "Memento Mori (Epic Edition)" are NOT — only the
  plain "Valhalla" / "Memento Mori" singles exist → must NOT match.
- "Gangnam Style (PSY Cover)" is a cover annotation — MusicBrainz omits it from
  the release-group title, so it must still match the plain "Gangnam Style"
  single.
"""

from __future__ import annotations

import re

import pytest

# ── Shared fake release-group catalogue (mirrors the live MB data) ──────────

_PLAIN_VALHALLA = {"id": "b9486692-eeab-4395-a466-458faa8b8124", "title": "Valhalla", "primary-type": "Single"}
_PLAIN_MEMENTO = {"id": "0c86bfc2-5fc7-46b4-a5f6-9035cfaa20d8", "title": "Memento Mori", "primary-type": "Single"}
_PLAIN_BASTARD = {"id": "c93cdacd-d4c0-492d-bc7f-cf1a2bd45361", "title": "Bastard von Asgard", "primary-type": "Single"}
_PLAIN_GANGNAM = {"id": "e55b7c9b-1d9b-495d-aa6f-4df87b90fdc9", "title": "Gangnam Style", "primary-type": "Single"}
_PLAIN_DAS_ELFTE = {"id": "2fc7c83d-8e52-4ba6-b3fe-842ff0d28da8", "title": "Das Elfte Gebot", "primary-type": "Single"}
_EPIC_DAS_ELFTE = {"id": "12870d14-4b82-4728-8984-0733e47d7b6d", "title": "Das Elfte Gebot (Epic Edition)", "primary-type": "Single"}

_ALL_SINGLES = [
    _PLAIN_VALHALLA, _PLAIN_MEMENTO, _PLAIN_BASTARD, _PLAIN_GANGNAM,
    _PLAIN_DAS_ELFTE, _EPIC_DAS_ELFTE,
]

# ``releasegroup:"<phrase>"`` queries only surface a group when the phrase is
# actually part of a release-group title (mirrors the verified MB behaviour:
# "das elfte gebot epic edition" matches the Epic Edition single, while
# "valhalla epic edition" matches nothing).
_PHRASE_RESULTS = {
    "das elfte gebot": [_PLAIN_DAS_ELFTE, _EPIC_DAS_ELFTE],
    "das elfte gebot epic edition": [_EPIC_DAS_ELFTE],
    "gangnam style": [_PLAIN_GANGNAM],
}


class _FakeMBClient:
    """Mimics ``MusicBrainzHttpClient`` for the artist-scoped single path."""

    def __init__(self, singles=None, phrase_results=None):
        self._singles = singles if singles is not None else _ALL_SINGLES
        self._phrase = phrase_results if phrase_results is not None else _PHRASE_RESULTS

    def search_release_groups(self, query, limit=10):
        if "releasegroup:" in query:
            m = re.search(r'releasegroup:"([^"]+)"', query)
            phrase = m.group(1) if m else ""
            return list(self._phrase.get(phrase, []))
        if "primarytype:" in query:
            return [g for g in self._singles]
        return []

    def is_single(self, *args, **kwargs):
        return False


def _detect_mb(title, artist, artist_mbid="107fe9d9-903d-4bdc-ba88-d1df3c2fa32c"):
    from services.enrichment.single_detection_service import _detect_musicbrainz

    return _detect_musicbrainz(
        title,
        artist,
        artist_mbid,
        album_track_count=20,
        mb_client=_FakeMBClient(),
    )


class TestEditionAnnotationHelpers:
    def test_extract_edition_annotation(self):
        from helpers.normalization_service import extract_edition_annotation

        assert extract_edition_annotation("Valhalla (Epic Edition)") == "epic edition"
        assert extract_edition_annotation("Das Elfte Gebot (Epic Edition)") == "epic edition"
        # Cover / live annotations are NOT editions.
        assert extract_edition_annotation("Gangnam Style (PSY Cover)") is None
        assert extract_edition_annotation("Knightclub (Live)") is None
        assert extract_edition_annotation("Knightclub") is None

    def test_edition_annotations_compatible(self):
        from helpers.normalization_service import edition_annotations_compatible

        # Edition-annotated track vs plain single: INCOMPATIBLE.
        assert not edition_annotations_compatible("Valhalla (Epic Edition)", "Valhalla")
        assert not edition_annotations_compatible("Valhalla", "Valhalla (Epic Edition)")
        # Same edition annotation on both sides: compatible.
        assert edition_annotations_compatible(
            "Valhalla (Epic Edition)", "Valhalla (Epic Edition)"
        )
        # Plain vs plain: compatible.
        assert edition_annotations_compatible("Valhalla", "Valhalla")
        # Cover annotation vs plain: compatible (MB omits cover annotations).
        assert edition_annotations_compatible("Gangnam Style (PSY Cover)", "Gangnam Style")


class TestDetectMusicBrainzEditionMatching:
    def test_epic_edition_with_real_epic_single_matches(self):
        # "Das Elfte Gebot (Epic Edition)" IS a single on MB.
        result = _detect_mb("Das Elfte Gebot (Epic Edition)", "Feuerschwanz")
        assert result["matched"] is True

    def test_epic_edition_with_only_plain_single_does_not_match(self):
        # "Valhalla (Epic Edition)" must NOT match the plain "Valhalla" single.
        result = _detect_mb("Valhalla (Epic Edition)", "Feuerschwanz feat. Doro")
        assert result["matched"] is False

    def test_epic_edition_memento_mori_does_not_match(self):
        result = _detect_mb("Memento Mori (Epic Edition)", "Feuerschwanz")
        assert result["matched"] is False

    def test_epic_edition_bastard_von_asgard_does_not_match(self):
        result = _detect_mb("Bastard von Asgard (Epic Edition)", "Feuerschwanz")
        assert result["matched"] is False

    def test_cover_annotation_still_matches_plain_single(self):
        # "(PSY Cover)" is a cover annotation, not an edition — MusicBrainz
        # omits it from the release-group title, so it still matches.
        result = _detect_mb("Gangnam Style (PSY Cover)", "Feuerschwanz")
        assert result["matched"] is True

    def test_plain_track_still_matches_plain_single(self):
        result = _detect_mb("Das Elfte Gebot", "Feuerschwanz")
        assert result["matched"] is True


class TestMusicBrainzServiceFallback:
    """The service fallback path (no artist MBID) has the same gate."""

    def _service(self):
        from services.enrichment.musicbrainz_service import MusicBrainzService

        class _FakeHTTP:
            def search_release_groups(self, query, limit=10):
                if "releasegroup:" in query:
                    return []
                return [dict(g) for g in _ALL_SINGLES]

        return MusicBrainzService(http_client=_FakeHTTP())

    def test_release_group_has_single_release_epic_gated(self):
        svc = self._service()
        assert svc._release_group_has_single_release("Valhalla (Epic Edition)", "Feuerschwanz") is False
        assert svc._release_group_has_single_release(
            "Das Elfte Gebot (Epic Edition)", "Feuerschwanz"
        ) is True
        assert svc._release_group_has_single_release("Gangnam Style (PSY Cover)", "Feuerschwanz") is True

    def test_rg_title_matches_epic_gated(self):
        svc = self._service()
        assert svc._rg_title_matches("Valhalla (Epic Edition)", "Valhalla") is False
        assert svc._rg_title_matches(
            "Valhalla (Epic Edition)", "Valhalla (Epic Edition)"
        ) is True
        assert svc._rg_title_matches("Gangnam Style (PSY Cover)", "Gangnam Style") is True
        assert svc._rg_title_matches("Valhalla", "Valhalla") is True
