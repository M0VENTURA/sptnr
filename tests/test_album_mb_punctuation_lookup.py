"""Regression tests for punctuation-heavy album-title MusicBrainz lookups.

Real-world case: ATEEZ "GOLDEN HOUR: Part.4".  MusicBrainz's release-group
index tokenises the title's colon/spacing differently from the stored value
("GOLDEN HOUR : Part.4"), so the QUOTED ``releasegroup:"GOLDEN HOUR: Part.4"``
phrase returns ZERO results even though the release-group exists (verified
against the live API 2026-08-16).  The lookup now falls back to an UNQUOTED
term query (``releasegroup:golden hour part 4``), which MusicBrainz ranks with
the exact group first; local re-scoring then picks it.

Fixture data is real MusicBrainz release-groups captured 2026-08-16:
- ATEEZ "GOLDEN HOUR : Part.4" (EP, 2026-02-06)
- Avenged Sevenfold "Diamonds in the Rough" (Album + Compilation, 2008-09-16)
"""

from __future__ import annotations

# Real MusicBrainz release-group payloads (id/title/type/artist-credit).
ATEEZ_RG_PART4 = {
    "id": "8072d29d-f779-404e-b5cc-0cc01e442444",
    "title": "GOLDEN HOUR : Part.4",
    "primary-type": "EP",
    "first-release-date": "2026-02-06",
    "artist-credit": [
        {"name": "ATEEZ", "artist": {"id": "cf0dbc16-4e24-4957-b5b4-955c8978f690", "name": "ATEEZ"}}
    ],
}

A7X_RG_DIAMONDS = {
    "id": "9c1a3548-33aa-44ee-a26f-3ce8b3be358f",
    "title": "Diamonds in the Rough",
    "primary-type": "Album",
    "secondary-types": ["Compilation"],
    "first-release-date": "2008-09-16",
    "artist-credit": [
        {"name": "Avenged Sevenfold", "artist": {"id": "24e1b53c-3085-4581-8472-0b0088d2508c", "name": "Avenged Sevenfold"}}
    ],
}


class _FakeMBHttp:
    """``search_release_groups`` distinguishes QUOTED (phrase) vs UNQUOTED
    (term) releasegroup queries, mirroring MusicBrainz's tokenisation
    behaviour for punctuation-heavy titles."""

    def __init__(self, quoted_results=None, unquoted_results=None):
        self.quoted_results = quoted_results if quoted_results is not None else []
        self.unquoted_results = unquoted_results if unquoted_results is not None else []
        self.queries: list[str] = []

    def search_release_groups(self, query, limit=10):
        self.queries.append(query)
        if 'releasegroup:"' in query:
            return list(self.quoted_results)
        return list(self.unquoted_results)


def _service(http):
    from services.enrichment.musicbrainz_service import MusicBrainzService
    return MusicBrainzService(http_client=http)


def test_normalize_title_for_lucene_query_strips_punctuation():
    from helpers.normalization_service import normalize_title_for_lucene_query
    # The colon is the failing token in the quoted phrase; the normalised
    # (punctuation-free) form is what the unquoted fallback sends.
    assert normalize_title_for_lucene_query("GOLDEN HOUR: Part.4") == "golden hour part 4"
    assert normalize_title_for_lucene_query("Diamonds in the Rough") == "diamonds in the rough"


def test_search_releasegroup_matches_falls_back_to_unquoted_terms():
    """ATEEZ 'GOLDEN HOUR: Part.4': the quoted phrase misses, the unquoted
    term fallback finds the exact release-group and it wins the match."""
    http = _FakeMBHttp(quoted_results=[], unquoted_results=[ATEEZ_RG_PART4])
    svc = _service(http)
    matches = svc.search_releasegroup_matches("ATEEZ", "GOLDEN HOUR: Part.4")

    assert matches, "fallback must surface the ATEEZ release-group"
    assert matches[0]["id"] == ATEEZ_RG_PART4["id"]
    assert matches[0]["title"] == "GOLDEN HOUR : Part.4"
    assert matches[0]["primary_type"] == "EP"
    assert matches[0]["match_score"] >= 0.8
    # The quoted query was attempted first; the unquoted fallback ran too.
    assert any('releasegroup:"' in q for q in http.queries)
    assert any("releasegroup:golden hour part 4" in q for q in http.queries)


def test_search_releasegroup_matches_quoted_path_unchanged():
    """Avenged Sevenfold 'Diamonds in the Rough' resolves via the quoted
    phrase alone — no fallback needed (regression guard)."""
    http = _FakeMBHttp(quoted_results=[A7X_RG_DIAMONDS], unquoted_results=[])
    svc = _service(http)
    matches = svc.search_releasegroup_matches("Avenged Sevenfold", "Diamonds in the Rough")

    assert matches
    assert matches[0]["id"] == A7X_RG_DIAMONDS["id"]
    assert matches[0]["title"] == "Diamonds in the Rough"
    # No unquoted fallback query should have been issued.
    assert not any("releasegroup:" in q and 'releasegroup:"' not in q for q in http.queries)
