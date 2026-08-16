"""Regression tests: version/alternate-take/bonus tracks resolve to their OWN
recording, anchored on the album being scanned.

Reproduces the Arion "Last Of Us" scan from #887, where ``(2018 Version)``
bonus tracks and plain album tracks resolved to the WRONG recording: the MB
batch resolver compared candidates with BRACKET-STRIPPING normalisation, so a
version-tagged track ("Last Of Us (2018 Version)") tied with its plain studio
sibling ("Last of Us") and resolved to whichever recording MusicBrainz
returned first — leaking that recording's ISRC/MBID and its ListenBrainz
counts onto the alternate take.

The correct resolution depends on the album (per #887 review):
- a REMASTERED reissue reuses the ORIGINAL recording — the "(2018 Version)"
  track is the same recording (same MBID/ISRC) as the original;
- a full RERECORDING carries its own recording (own MBID/ISRC).

MusicBrainz encodes this in the recording/release graph (same recording on a
remaster release, different recording on a rerecording), so the resolver
anchors on the scanned album's embedded releases to pick the recording that
actually appears on that album.
"""

from __future__ import annotations

import pytest


def _fake_http(recordings):
    """A ``MusicBrainzHttpClient`` stand-in whose search returns the whole
    catalogue for every query — mirroring the real album batch, where the OR
    query mixes every track's candidates into ONE result set."""
    class _FakeHTTP:
        def __init__(self, catalogue):
            self._catalogue = catalogue

        def search_recordings(self, query, limit=10, inc=""):
            return [dict(r) for r in self._catalogue]

    return _FakeHTTP(recordings)


def _service(http):
    from services.enrichment.musicbrainz_service import MusicBrainzService
    return MusicBrainzService(http_client=http)


def _rel(title, rg_title=None):
    release = {"title": title}
    if rg_title is not None:
        release["release-group"] = {"title": rg_title}
    return release


# ── Arion – "Last Of Us" (2018) ──────────────────────────────────────────────
# The re-recorded album carries its OWN "(2018 Version)" recordings for the
# old singles (distinct ISRCs) while the plain album tracks are new songs.

_ARION_CATALOGUE = [
    {
        "id": "break-dawn",
        "title": "At The Break Of Dawn",
        "isrcs": ["FIRAN1800001"],
        "releases": [_rel("Last Of Us")],
    },
    {
        "id": "break-dawn-2018",
        "title": "At The Break Of Dawn (2018 Version)",
        "isrcs": ["FIRAN1800002"],
        "releases": [_rel("Last Of Us")],
    },
    {
        "id": "last-of-us",
        "title": "Last of Us",
        "isrcs": ["FIRORIG0001"],
        "releases": [_rel("Last of Us")],
    },
    {
        "id": "last-of-us-2018",
        "title": "Last of Us (2018 Version)",
        "isrcs": ["FIRAN1800004"],
        "releases": [_rel("Last Of Us")],
    },
    {
        "id": "seven-2018",
        "title": "Seven (2018 Version)",
        "isrcs": ["FIRAN1800005"],
        "releases": [_rel("Last Of Us")],
    },
    {
        "id": "end-of-fall",
        "title": "The End Of The Fall",
        "isrcs": ["FIREOF0001"],
        "releases": [_rel("Last Of Us")],
    },
    {
        # The same track also appears on a compilation — the resolver must
        # prefer the recording on the scanned album, not this one.
        "id": "end-of-fall-comp",
        "title": "The End Of The Fall",
        "isrcs": ["FIREOF0002"],
        "releases": [_rel("The Best Of Arion")],
    },
]


class TestAlbumBatchVersionResolution:
    def test_version_bonus_track_resolves_to_its_own_recording(self):
        svc = _service(_fake_http(_ARION_CATALOGUE))
        batch = svc.lookup_album_metadata(
            [("Last of Us (2018 Version)", "Arion")],
            album="Last Of Us",
        )
        meta = batch["arion::last of us (2018 version)"]
        # Its OWN recording — not the plain original single, and not any
        # other "(2018 Version)" track's recording.
        assert meta["recording_mbid"] == "last-of-us-2018"
        assert meta["isrc"] == "FIRAN1800004"

    def test_plain_track_does_not_absorb_the_version_recording(self):
        svc = _service(_fake_http(_ARION_CATALOGUE))
        batch = svc.lookup_album_metadata(
            [("At The Break Of Dawn", "Arion")],
            album="Last Of Us",
        )
        meta = batch["arion::at the break of dawn"]
        assert meta["recording_mbid"] == "break-dawn"
        assert meta["isrc"] == "FIRAN1800001"

    def test_same_title_prefers_the_album_anchored_recording(self):
        # Two recordings share the plain title "The End Of The Fall"; the one
        # that appears on the scanned album wins over the compilation cut.
        svc = _service(_fake_http(_ARION_CATALOGUE))
        batch = svc.lookup_album_metadata(
            [("The End Of The Fall", "Arion")],
            album="Last Of Us",
        )
        meta = batch["arion::the end of the fall"]
        assert meta["recording_mbid"] == "end-of-fall"
        assert meta["isrc"] == "FIREOF0001"

    def test_no_album_still_prefers_highest_title_similarity(self):
        # Without album context the version track still beats the plain
        # sibling because candidate comparison is bracket-preserving.
        svc = _service(_fake_http(_ARION_CATALOGUE))
        batch = svc.lookup_album_metadata(
            [("Seven (2018 Version)", "Arion")],
        )
        meta = batch["arion::seven (2018 version)"]
        assert meta["recording_mbid"] == "seven-2018"


# ── Remaster vs rerecording: the album decides ───────────────────────────────

def _remaster_catalogue():
    """A REMASTERED album: the "(2018 Version)" track is the ORIGINAL
    recording (same MBID/ISRC) — MusicBrainz reuses it on the reissue."""
    return [
        {
            "id": "orig-rec",
            "title": "Last of Us",
            "isrcs": ["FIRORIG0001"],
            "releases": [
                _rel("Last of Us"),            # the original single
                _rel("Last Of Us (2018 Remaster)"),  # the scanned reissue
            ],
        }
    ]


class TestRemasterVersusRerecording:
    def test_remaster_album_resolves_version_track_to_the_original(self):
        # Local file says "(2018 Version)" but the album is a REMASTER, so the
        # track is the ORIGINAL recording — same ISRC as the plain track.
        svc = _service(_fake_http(_remaster_catalogue()))
        batch = svc.lookup_album_metadata(
            [("Last of Us (2018 Version)", "Arion")],
            album="Last Of Us (2018 Remaster)",
        )
        meta = batch["arion::last of us (2018 version)"]
        assert meta["recording_mbid"] == "orig-rec"
        assert meta["isrc"] == "FIRORIG0001"

    def test_rerecording_album_resolves_version_track_to_its_own(self):
        # The rerecording case is covered by the Arion catalogue: the
        # "(2018 Version)" cut has a DIFFERENT recording and ISRC.
        svc = _service(_fake_http(_ARION_CATALOGUE))
        batch = svc.lookup_album_metadata(
            [("Last of Us (2018 Version)", "Arion")],
            album="Last Of Us",
        )
        meta = batch["arion::last of us (2018 version)"]
        assert meta["recording_mbid"] == "last-of-us-2018"
        assert meta["isrc"] == "FIRAN1800004"


# ── Edition gating (Epic Edition / Deluxe) ───────────────────────────────────

_EDITION_CATALOGUE = [
    {
        "id": "plain-valhalla",
        "title": "Valhalla",
        "isrcs": ["FIREPIC0001"],
        "releases": [_rel("Valhalla", rg_title="Valhalla")],
    },
    {
        "id": "epic-valhalla",
        "title": "Valhalla (Epic Edition)",
        "isrcs": ["FIREPIC0002"],
        "releases": [_rel("Valhalla (Epic Edition)", rg_title="Valhalla (Epic Edition)")],
    },
]


class TestEditionGating:
    def test_edition_track_never_resolves_to_plain_recording(self):
        svc = _service(_fake_http(_EDITION_CATALOGUE))
        batch = svc.lookup_album_metadata(
            [("Valhalla (Epic Edition)", "Feuerschwanz")],
            album="Valhalla (Epic Edition)",
        )
        meta = batch["feuerschwanz::valhalla (epic edition)"]
        assert meta["recording_mbid"] == "epic-valhalla"
        assert meta["isrc"] == "FIREPIC0002"

    def test_plain_track_resolves_to_plain_recording(self):
        svc = _service(_fake_http(_EDITION_CATALOGUE))
        batch = svc.lookup_album_metadata(
            [("Valhalla", "Feuerschwanz")],
            album="Valhalla",
        )
        meta = batch["feuerschwanz::valhalla"]
        assert meta["recording_mbid"] == "plain-valhalla"
        assert meta["isrc"] == "FIREPIC0001"


class TestSuggestedMbidTieBreak:
    """``get_suggested_mbid`` has the same bracket-preserving comparison."""

    def test_live_track_resolves_to_its_own_recording(self):
        # "Time Is Running Out (Live)" must not resolve to the studio cut even
        # when the studio cut is returned first by the search.
        catalogue = [
            {
                "id": "studio",
                "title": "Time Is Running Out",
                "releases": [_rel("Absolution")],
            },
            {
                "id": "live",
                "title": "Time Is Running Out (Live)",
                "releases": [_rel("Hullabaloo Soundtrack")],
            },
        ]
        svc = _service(_fake_http(catalogue))
        mbid, confidence = svc.get_suggested_mbid(
            "Time Is Running Out (Live)", "Muse"
        )
        assert mbid == "live"
        assert confidence > 0.9

    def test_plain_track_still_resolves_to_studio(self):
        catalogue = [
            {
                "id": "studio",
                "title": "Time Is Running Out",
                "releases": [_rel("Absolution")],
            },
            {
                "id": "live",
                "title": "Time Is Running Out (Live)",
                "releases": [_rel("Hullabaloo Soundtrack")],
            },
        ]
        svc = _service(_fake_http(catalogue))
        mbid, _ = svc.get_suggested_mbid("Time Is Running Out", "Muse")
        assert mbid == "studio"