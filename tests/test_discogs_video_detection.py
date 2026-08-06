"""Regression tests: Discogs official-video single evidence.

The legacy scanner confirmed singles via the track's official/promo music
video on Discogs (``has_official_video``). The new engine's
``discogs_video`` source was plumbed into the confidence decision but never
actually detected — ``discogs_video_confirmed`` was always False. These
tests cover the ported matcher: an official/promo keyword (whole word) plus
an exact cleaned-title match against the track.
"""

from __future__ import annotations

from services.enrichment.discogs_service import DiscogsService


def _match(video_title: str, track_title: str, video_desc: str = "") -> bool:
    return DiscogsService._is_official_video_for_track(
        {"title": video_title, "description": video_desc}, track_title.lower()
    )


def test_official_video_matches():
    assert _match("No, It Isnt (Official Video)", "No, It Isn't")
    assert _match("Baby Come On [Official Music Video]", "Baby Come On")
    assert _match("155 - Official Video", "155")
    assert _match("Lycanthrope (Official Video)", "Lycanthrope")


def test_artist_prefix_is_stripped():
    assert _match("+44 - 155 (Official Music Video)", "155")
    assert _match("+44 - Baby Come On (Official Video)", "Baby Come On")


def test_promo_counts_as_official():
    assert _match("155 (Promo Video)", "155")


def test_unofficial_is_rejected():
    assert not _match("155 (Unofficial Video)", "155")
    assert not _match("155 (Unofficial)", "155")


def test_no_keyword_is_rejected():
    assert not _match("155 (Video)", "155")
    assert not _match("155", "155")  # plain title, no official/promo marker


def test_title_must_match_exactly():
    assert not _match("Lycanthrope (Official Video)", "155")
    assert not _match("When Your Heart Stops Beating II (Official Video)", "When Your Heart Stops Beating")


def test_description_never_matches_alone():
    # "official" appears in the description, but the cleaned description
    # does not equal the track title — legacy parity, no false positive.
    assert not _match("Some Clip", "155", "Official video for 155 by +44")
