"""
Genre detection service.

Detects special genre tags (Christmas, Cover, Live, Acoustic, Orchestral,
Instrumental, Remix) based on track metadata, audio features, and
title/album analysis.
"""

from __future__ import annotations

import json
import re
from typing import Any


class GenreDetector:
    """Detects special genre tags for tracks based on metadata and audio features."""

    # ── Keyword sets ──────────────────────────────────────────────────────

    CHRISTMAS_KEYWORDS = frozenset({
        "christmas", "xmas", "holiday", "noel", "santa", "sleigh",
        "jingle", "silent night", "holy night", "winter wonderland",
        "deck the halls", "carol", "advent",
    })

    COVER_KEYWORDS_TITLE = frozenset({
        "(cover)", "(tribute)", "(originally by)", "cover version",
        "tribute to", "in the style of",
    })

    COVER_KEYWORDS_ALBUM = frozenset({
        "tribute", "covers", "tribute to", "covering", "in the style",
    })

    LIVE_KEYWORDS_TITLE = frozenset({
        "(live)", "live at", "live from", "- live", " live ", "live version",
    })

    LIVE_KEYWORDS_ALBUM = frozenset({
        "unplugged", "live at", "live from", "in concert",
        "live session", "bbc live", "live in", "live tour", "(live)", "[live]",
    })

    ACOUSTIC_KEYWORDS = frozenset({
        "(acoustic)", "acoustic version", "- acoustic", " acoustic ",
    })

    REMIX_KEYWORDS_TITLE = frozenset({
        "(remix)", " remix", "- remix", "remix version", "remixed", "remix edit",
    })

    REMIX_KEYWORDS_ALBUM = frozenset({
        "remix", "remixes", "remixed", "remix album", "(remix)", "+remix",
    })

    ORCHESTRAL_KEYWORDS = frozenset({
        "orchestral", "symphonic", "symphony", "philharmonic",
        "orchestra", "orchestrated",
    })

    # ── Public API ────────────────────────────────────────────────────────

    def detect_special_tags(
        self,
        track_name: str,
        album_name: str,
        artist_genres: list[str] | None = None,
        audio_features: dict | None = None,
        album_type: str | None = None,
    ) -> set[str]:
        """Detect all special genre tags for a track.

        Args:
            track_name: Track title.
            album_name: Album name.
            artist_genres: List of artist genres from Spotify.
            audio_features: Dict of audio features (acousticness, liveness, …).
            album_type: Album type string (may contain ``+live``, ``(remix)``, etc.).

        Returns:
            Set of detected special tags (e.g. ``{"Live", "Acoustic"}``).
        """
        tags: set[str] = set()
        track_lower = (track_name or "").lower()
        album_lower = (album_name or "").lower()
        genres_lower = [g.lower() for g in (artist_genres or [])]

        if self._detect_christmas(track_lower, album_lower, genres_lower):
            tags.add("Christmas")
        if self._detect_cover(track_lower, album_lower):
            tags.add("Cover")
        if self._detect_live(track_lower, album_lower, audio_features, album_type):
            tags.add("Live")
        if self._detect_acoustic(track_lower, audio_features):
            tags.add("Acoustic")
        if self._detect_remix(track_lower, album_lower, album_type):
            tags.add("Remix")

        orchestral, instrumental = self._detect_orchestral_instrumental(
            track_lower, audio_features,
        )
        if orchestral:
            tags.add("Orchestral")
        if instrumental:
            tags.add("Instrumental")

        return tags

    # ── Internal detectors ────────────────────────────────────────────────

    @staticmethod
    def _detect_christmas(track_lower: str, album_lower: str, genres_lower: list[str]) -> bool:
        for kw in GenreDetector.CHRISTMAS_KEYWORDS:
            if kw in track_lower or kw in album_lower:
                return True
        for genre in genres_lower:
            if "christmas" in genre or "holiday" in genre:
                return True
        return False

    @staticmethod
    def _detect_cover(track_lower: str, album_lower: str) -> bool:
        for kw in GenreDetector.COVER_KEYWORDS_TITLE:
            if kw in track_lower:
                return True
        for kw in GenreDetector.COVER_KEYWORDS_ALBUM:
            if kw in album_lower:
                return True
        return False

    @staticmethod
    def _detect_live(
        track_lower: str,
        album_lower: str,
        audio_features: dict | None,
        album_type: str | None,
    ) -> bool:
        if album_type:
            t = album_type.lower()
            if "+live" in t or "(live)" in t:
                return True

        for kw in GenreDetector.LIVE_KEYWORDS_TITLE:
            if kw in track_lower:
                return True

        live_patterns = [
            r"\blive\s+at\b", r"\blive\s+in\b", r"\blive\s+from\b",
            r"\blive\s+session\b", r"\blive\s+tour\b",
            r"\(live\)", r"\[live\]", r"-\s*live\b", r"\s+live\s*$",
            r"\bconcert\b", r"\bin\s+concert\b",
        ]
        for pat in live_patterns:
            if re.search(pat, album_lower):
                return True
        for kw in GenreDetector.LIVE_KEYWORDS_ALBUM:
            if kw in album_lower:
                return True

        if audio_features and audio_features.get("liveness", 0) > 0.8:
            return True
        return False

    @staticmethod
    def _detect_acoustic(track_lower: str, audio_features: dict | None) -> bool:
        for kw in GenreDetector.ACOUSTIC_KEYWORDS:
            if kw in track_lower:
                return True
        if audio_features and audio_features.get("acousticness", 0) > 0.7:
            return True
        return False

    @staticmethod
    def _detect_remix(
        track_lower: str, album_lower: str, album_type: str | None,
    ) -> bool:
        if album_type:
            t = album_type.lower()
            if "+remix" in t or "(remix)" in t:
                return True
        for kw in GenreDetector.REMIX_KEYWORDS_TITLE:
            if kw in track_lower:
                return True
        for kw in GenreDetector.REMIX_KEYWORDS_ALBUM:
            if kw in album_lower:
                return True
        return False

    @staticmethod
    def _detect_orchestral_instrumental(
        track_lower: str, audio_features: dict | None,
    ) -> tuple[bool, bool]:
        is_orchestral = False
        is_instrumental = False

        for kw in GenreDetector.ORCHESTRAL_KEYWORDS:
            if kw in track_lower:
                is_orchestral = True
                break

        if audio_features:
            instr = audio_features.get("instrumentalness", 0)
            acous = audio_features.get("acousticness", 0)
            if instr > 0.8:
                is_instrumental = True
            if instr > 0.8 and acous > 0.5:
                is_orchestral = True

        return is_orchestral, is_instrumental

    # ── Genre normalization ───────────────────────────────────────────────

    @staticmethod
    def normalize_genres(artist_genres: list[str] | None) -> list[str]:
        """Map raw artist genres to broad categories."""
        if not artist_genres:
            return []

        genre_map: dict[str, tuple[str, ...]] = {
            "rock": ("rock", "alternative", "indie", "grunge", "punk"),
            "metal": ("metal", "metalcore", "death metal", "black metal"),
            "pop": ("pop", "dance pop", "electropop", "synth-pop"),
            "electronic": ("electronic", "edm", "techno", "house", "dubstep", "drum and bass"),
            "hip hop": ("hip hop", "rap", "trap", "hip-hop"),
            "jazz": ("jazz", "bebop", "smooth jazz", "jazz fusion"),
            "classical": ("classical", "baroque", "romantic"),
            "country": ("country", "americana", "bluegrass"),
            "r&b": ("r&b", "soul", "funk", "neo soul"),
            "folk": ("folk", "folk rock", "singer-songwriter"),
        }

        normalized: set[str] = set()
        for genre in artist_genres:
            g = genre.lower()
            for broad, keywords in genre_map.items():
                if any(kw in g for kw in keywords):
                    normalized.add(broad)
        return sorted(normalized)


# Singleton for convenience
_detector = GenreDetector()


def detect_special_tags(
    track_name: str,
    album_name: str,
    artist_genres: list[str] | None = None,
    audio_features: dict | None = None,
    album_type: str | None = None,
) -> set[str]:
    """Convenience wrapper — uses the singleton GenreDetector."""
    return _detector.detect_special_tags(
        track_name, album_name, artist_genres, audio_features, album_type,
    )


# =============================================================================
# GENRE / TITLE PROCESSING
# =============================================================================

_PARENTHETICAL_TAG_RE = re.compile(r"\(([^)]+)\)")


def _has_parenthetical_tag(title: str, tag: str) -> bool:
    """Check if *title* contains *tag* inside parentheses (case-insensitive)."""
    if not title or not tag:
        return False
    return bool(re.search(r"\(" + re.escape(tag) + r"[^)]*\)", title, re.IGNORECASE))


def process_track_genres_and_title(
    track_title: str,
    album_name: str,
    genre_list: list[str],
) -> tuple[str, list[str]]:
    """Process track title and genres based on metadata hints.

    Rules:
    1. If genre has ``acoustic`` / ``live``, append to title if not already present.
    2. If title has ``(Live)`` / ``(Acoustic)`` / ``(Demo)`` / ``(Remix)`` / ``(Unplugged)``,
       add them to genre list.
    3. If album has ``acoustic`` / ``unplugged``, propagate to title and genres.

    Returns:
        Tuple of (updated_title, updated_genre_list).
    """
    updated_title = track_title
    updated_genres = list(genre_list)

    # ── Step 1: Extract tags from title and add to genres ────────────────
    for tag in ("live", "unplugged", "acoustic", "demo", "remix"):
        if _has_parenthetical_tag(track_title, tag):
            capitalized = tag.capitalize()
            if not any(capitalized.lower() in g.lower() for g in updated_genres):
                updated_genres.append(capitalized)

    # ── Step 2: Check album for acoustic/unplugged and propagate ──────────
    album_lower = (album_name or "").lower()
    album_has_acoustic = "acoustic" in album_lower
    album_has_unplugged = "unplugged" in album_lower
    album_has_live = bool(re.search(r"\blive\b", album_lower))

    for tag_name, has_tag in (
        ("acoustic", album_has_acoustic),
        ("unplugged", album_has_unplugged),
    ):
        if has_tag:
            tag_cap = tag_name.capitalize()
            if not any(tag_name in g.lower() for g in updated_genres):
                updated_genres.append(tag_cap)
            if not _has_parenthetical_tag(updated_title, tag_name):
                updated_title = f"{updated_title} ({tag_name})"

    # ── Step 3: Append acoustic/live/unplugged to title from genre ───────
    for tag in ("acoustic", "live", "unplugged"):
        has_genre = any(tag in g.lower() for g in updated_genres)
        already_in_title = _has_parenthetical_tag(updated_title, tag)
        if has_genre and not already_in_title:
            updated_title = f"{updated_title} ({tag})"

    return updated_title, updated_genres


# Singleton for convenience
_detector = GenreDetector()
