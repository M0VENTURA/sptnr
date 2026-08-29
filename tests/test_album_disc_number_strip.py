"""Regression: saving album metadata must strip disc_number on single-disc
albums (disctotal <= 1) and default it to "1" on multi-disc albums.

The album save handler previously wrote ``disctotal`` to tracks but never
derived per-track ``disc_number``, so a single-disc album kept a bogus "1"
(or worse) disc position in the DB and file tags.  This test pins the
derivation logic used in ``routes/ui_routes.py``'s album save loop.
"""

from __future__ import annotations


def _derive_disc_payload(disctotal_raw: str, track_disc_number: str | None, max_track_disc: int = 1) -> dict[str, str] | None:
    """Mirror of the route's disc logic: returns the disc_number payload to
    add (or None when unchanged).

    ``max_track_disc`` mirrors the album's highest track disc_number (used to
    infer single- vs multi-disc when ``disctotal`` is empty — the reported
    "one track has disc 1, the rest are empty — keeps the 1" bug).
    """
    _strip = False
    _multi = False
    try:
        _dt = int(disctotal_raw)
        _multi = _dt > 1
        _strip = _dt <= 1
    except (TypeError, ValueError):
        _strip = bool(disctotal_raw)

    # Empty disctotal: infer from the actual track disc numbers.  A stray
    # "1" with no disc > 1 means single-disc → strip; any disc > 1 means
    # multi-disc → keep/default.
    if not disctotal_raw and not _multi:
        if max_track_disc <= 1:
            _strip = True
        elif max_track_disc > 1:
            _multi = True

    cur = str(track_disc_number or "").strip()
    if _strip:
        if cur:
            return {"disc_number": ""}
        return None
    if _multi:
        if not cur:
            return {"disc_number": "1"}
        return None
    return None


class TestAlbumDiscNumberDerivation:
    def test_single_disc_strips_existing(self):
        """disctotal=1 clears an existing disc_number (DB + file tag)."""
        assert _derive_disc_payload("1", "1") == {"disc_number": ""}
        assert _derive_disc_payload("1", "1/2") == {"disc_number": ""}

    def test_single_disc_noop_when_already_empty(self):
        assert _derive_disc_payload("1", "") is None
        assert _derive_disc_payload("1", None) is None

    def test_multi_disc_defaults_unset_to_1(self):
        assert _derive_disc_payload("2", "") == {"disc_number": "1"}
        assert _derive_disc_payload("2", None) == {"disc_number": "1"}

    def test_multi_disc_keeps_existing(self):
        assert _derive_disc_payload("2", "2") is None

    def test_empty_disctotal_strips_stray_disc1(self):
        """The reported bug: no disctotal provided but one track carries a
        disc_number of "1" (the rest empty) → the stray "1" is stripped so
        the album does not keep showing disc 1."""
        assert _derive_disc_payload("", "1", max_track_disc=1) == {"disc_number": ""}
        assert _derive_disc_payload("", "", max_track_disc=1) is None

    def test_empty_disctotal_multi_disc_keeps(self):
        """No disctotal but a track is on disc 2 → multi-disc; unset tracks
        default to "1" and existing values are kept."""
        assert _derive_disc_payload("", "", max_track_disc=2) == {"disc_number": "1"}
        assert _derive_disc_payload("", "2", max_track_disc=2) is None
