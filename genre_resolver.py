#!/usr/bin/env python3
"""
Genre Resolver - Resolves the top 3 genres for a track from multiple sources.

All genres are saved to source-specific database columns. During metadata scans,
this module resolves the top 3 genres based on cross-source frequency and
fallback priority, then updates the track's main ``genres`` column and file tags.

Priority order for fallback when a genre appears in only one source:
1. musicbrainz_genres
2. discogs_genres
3. lastfm_tags
4. essentia_genres

Manual genres are stored in ``manual_genres`` and are always preserved across
scans. If a manual genre is present, it is appended to the resolved top 3 so
the total may exceed 3, but only because of user additions.
"""

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Priority order for fallback when a genre appears in only one source
_SOURCE_PRIORITY = {
    "musicbrainz": 0,
    "discogs": 1,
    "lastfm": 2,
    "essentia": 3,
}


def _extract_genre_names(value: Any) -> List[str]:
    """Extract genre names from various database formats."""
    if not value:
        return []

    # JSON array of dicts or strings
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                result = []
                for item in parsed:
                    if isinstance(item, dict):
                        name = item.get("name", "")
                    elif isinstance(item, str):
                        name = item
                    else:
                        continue
                    if name:
                        result.append(name.strip())
                return result
        except (json.JSONDecodeError, TypeError):
            pass

    # Plain string (semicolon, comma, or backslash separated)
    text = str(value).strip()
    if not text or text in ("[]", "null", "None", ""):
        return []

    # Replace delimiters with commas and split
    text = text.replace("\\", ",").replace(";", ",")
    return [g.strip() for g in text.split(",") if g.strip()]


def _normalize_genre(genre: str) -> str:
    """Normalize genre for case-insensitive comparison."""
    return genre.lower().strip()


def resolve_track_genres(track_dict: Dict[str, Any]) -> List[str]:
    """
    Resolve the top 3 genres for a track from all available sources.

    Args:
        track_dict: Dictionary with track data from the database.

    Returns:
        List of genre names (max 3 from automatic sources, plus any manual
        genres that are not already present).
    """
    # Extract genres from each source
    sources = {
        "musicbrainz": _extract_genre_names(track_dict.get("musicbrainz_genres")),
        "discogs": _extract_genre_names(track_dict.get("discogs_genres")),
        "lastfm": _extract_genre_names(track_dict.get("lastfm_tags")),
        "essentia": _extract_genre_names(track_dict.get("essentia_genres")),
    }

    # Exclude any Essentia genre labels that are already stored in the mood
    # field. Essentia-to-Metadata writes mood tags to the MOOD tag and genre
    # tags to the GENRE tag, but a misconfigured or buggy version may write
    # mood values into GENRE. This filter prevents moods from polluting the
    # resolved top-genres list.
    if sources["essentia"] and track_dict.get("mood"):
        _mood_labels = {
            _normalize_genre(m)
            for m in str(track_dict.get("mood")).split(";")
            if m.strip()
        }
        sources["essentia"] = [
            g for g in sources["essentia"]
            if _normalize_genre(g) not in _mood_labels
        ]

    # Count frequency across sources (case-insensitive)
    genre_data: Dict[str, Dict[str, Any]] = {}
    for source_name, genres in sources.items():
        for genre in genres:
            key = _normalize_genre(genre)
            if key not in genre_data:
                genre_data[key] = {
                    "name": genre,
                    "count": 0,
                    "sources": [],
                }
            genre_data[key]["count"] += 1
            if source_name not in genre_data[key]["sources"]:
                genre_data[key]["sources"].append(source_name)

    # Sort by count descending, then by best source priority
    def _sort_key(item):
        _key, data = item
        count = data["count"]
        best_source_priority = min(
            _SOURCE_PRIORITY.get(s, 99) for s in data["sources"]
        )
        return (-count, best_source_priority, data["name"].lower())

    sorted_genres = sorted(genre_data.items(), key=_sort_key)

    # Select top 3
    top_genres = [data["name"] for _, data in sorted_genres[:3]]

    # Add manual genres (always preserved, deduplicated)
    manual_genres = _extract_genre_names(track_dict.get("manual_genres"))
    for genre in manual_genres:
        if genre not in top_genres:
            top_genres.append(genre)

    return top_genres


def format_genres_for_db(genres: List[str]) -> str:
    """Format a list of genres for the database ``genres`` column."""
    return ", ".join(genres) if genres else ""
