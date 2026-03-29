#!/usr/bin/env python3
"""
Genre and tag aggregation utilities for displaying tags on track, album, and artist pages.
Handles parsing JSON tag data from database and aggregating across multiple sources.
"""

import json
import logging
import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def parse_json_tags(json_str: str) -> list:
    """
    Parse JSON tag string from database.
    
    Args:
        json_str: JSON string from database column (e.g., lastfm_tags, discogs_genres)
        
    Returns:
        List of tag dicts with 'name' and optionally 'count' keys
        Returns empty list if invalid/None
    """
    if not json_str:
        return []
    
    try:
        data = json.loads(json_str)
        if not isinstance(data, list):
            return []
        return data
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug(f"Failed to parse JSON tags: {e}")
        return []


def extract_tag_names(tags_list: list) -> list:
    """
    Extract just the tag names from a list of tag objects.
    
    Args:
        tags_list: List of dicts with 'name' key or list of strings
        
    Returns:
        List of tag name strings
    """
    names = []
    for tag in tags_list:
        if isinstance(tag, dict):
            name = tag.get("name", "")
        elif isinstance(tag, str):
            name = tag
        else:
            continue
        
        if name:
            names.append(name)
    
    return names


def get_track_genres_and_tags(track_dict: dict) -> Dict[str, list]:
    """
    Extract all genre/tag sources from a single track dictionary.
    
    Args:
        track_dict: Dictionary with track data from database
        
    Returns:
        Dict mapping source name to list of tags
        Example: {
            'lastfm_tags': [{'name': 'rock', 'count': 100}, ...],
            'listenbrainz_genres': [{'name': 'alternative rock', 'count': 80}, ...],
            'discogs_genres': [{'name': 'Rock', ...}, ...],
            'spotify_genres': [{'name': 'rock', ...}, ...],
            'musicbrainz_genres': [...]
        }
    """
    sources = {}
    
    # Parse each source's tags
    # Use full column names as keys to match frontend expectations
    if track_dict.get("lastfm_tags"):
        sources["lastfm_tags"] = parse_json_tags(track_dict["lastfm_tags"])
    
    if track_dict.get("listenbrainz_genres"):
        sources["listenbrainz_genres"] = parse_json_tags(track_dict["listenbrainz_genres"])
    
    if track_dict.get("discogs_genres"):
        sources["discogs_genres"] = parse_json_tags(track_dict["discogs_genres"])
    
    if track_dict.get("spotify_genres"):
        sources["spotify_genres"] = parse_json_tags(track_dict["spotify_genres"])
    
    if track_dict.get("musicbrainz_genres"):
        sources["musicbrainz_genres"] = parse_json_tags(track_dict["musicbrainz_genres"])

    # Mood tags are stored in a dedicated column and can be JSON arrays,
    # semicolon-separated strings, or comma-separated strings.
    if track_dict.get("mood"):
        mood_tags = parse_mood_values(track_dict.get("mood"))
        if mood_tags:
            sources["mood"] = [{"name": mood, "count": 1} for mood in mood_tags]
    
    return sources


def parse_mood_values(mood_value) -> list:
    """Normalize a track mood field into a list of mood labels."""
    if mood_value is None:
        return []

    moods = []

    # Accept values stored as JSON arrays or simple delimiters.
    if isinstance(mood_value, list):
        raw_items = mood_value
    else:
        text = str(mood_value).strip()
        if not text:
            return []

        raw_items = None
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    raw_items = parsed
            except (json.JSONDecodeError, TypeError):
                raw_items = None

        if raw_items is None:
            text = text.replace("\\", ",")
            raw_items = re.split(r"[;,]", text)

    seen = set()
    for item in raw_items:
        mood = str(item).strip()
        if not mood:
            continue
        key = mood.lower()
        if key in seen:
            continue
        seen.add(key)
        moods.append(mood)

    return moods


def aggregate_tags_with_counts(track_list: list) -> Dict[str, Counter]:
    """
    Aggregate tags from multiple tracks, counting frequency across all sources.
    
    Args:
        track_list: List of track dictionaries from database
        
    Returns:
        Dict mapping source name to Counter of tag names and their counts
        Example: {
            'lastfm_tags': Counter({'rock': 5, 'alternative': 3, ...}),
            'listenbrainz_genres': Counter({...}),
            ...
        }
    """
    aggregated = defaultdict(Counter)
    
    for track in track_list:
        sources = get_track_genres_and_tags(track)
        
        for source_name, tags_list in sources.items():
            for tag in tags_list:
                # Extract tag name (could be dict with 'name' key or string)
                if isinstance(tag, dict):
                    tag_name = tag.get("name", "")
                    # If tag has a 'count' field, use it; otherwise increment by 1
                    count = tag.get("count", 1)
                    # Convert string counts to int
                    try:
                        count = int(count) if isinstance(count, str) else count
                    except (ValueError, TypeError):
                        count = 1
                elif isinstance(tag, str):
                    tag_name = tag
                    count = 1
                else:
                    continue
                
                if tag_name:
                    aggregated[source_name][tag_name] += count
    
    return aggregated


def get_top_tags(aggregated: Dict[str, Counter], limit: int = 20) -> Dict[str, list]:
    """
    Get top N tags from aggregated data.
    
    Args:
        aggregated: Output from aggregate_tags_with_counts()
        limit: Maximum number of top tags per source
        
    Returns:
        Dict mapping source name to list of (tag_name, count) tuples, sorted by count descending
    """
    top_tags = {}
    
    for source_name, counter in aggregated.items():
        # Get most common N items
        top_items = counter.most_common(limit)
        top_tags[source_name] = top_items
    
    return top_tags


def format_tags_for_display(top_tags: Dict[str, list]) -> Dict[str, list]:
    """
    Format top tags for HTML/template display.
    
    Args:
        top_tags: Output from get_top_tags()
        
    Returns:
        Dict mapping source name to list of dicts with 'name' and 'count' keys
    """
    formatted = {}
    
    for source_name, tag_list in top_tags.items():
        formatted[source_name] = [
            {"name": tag, "count": count}
            for tag, count in tag_list
        ]
    
    return formatted


def get_track_genres_summary(track_dict: dict) -> Dict[str, list]:
    """
    Get a summary of all genres/tags for a single track, formatted for display.
    
    Args:
        track_dict: Dictionary with track data from database
        
    Returns:
        Dict mapping source name to list of formatted tags
    """
    sources = get_track_genres_and_tags(track_dict)
    formatted = {}
    
    for source_name, tags_list in sources.items():
        # Keep original format (preserve counts if present)
        formatted[source_name] = tags_list
    
    return formatted


def get_album_genres_summary(track_list: list, limit: int = 20) -> Dict[str, list]:
    """
    Get aggregated genre/tag summary for an album (all its tracks).
    
    Args:
        track_list: List of track dictionaries from database
        limit: Maximum number of top tags per source to return
        
    Returns:
        Dict mapping source name to list of top tags with counts
    """
    aggregated = aggregate_tags_with_counts(track_list)
    top_tags = get_top_tags(aggregated, limit)
    return format_tags_for_display(top_tags)


def get_artist_genres_summary(track_list: list, limit: int = 30) -> Dict[str, list]:
    """
    Get aggregated genre/tag summary for an artist (all tracks across all albums).
    
    Args:
        track_list: List of ALL track dictionaries for the artist from database
        limit: Maximum number of top tags per source to return
        
    Returns:
        Dict mapping source name to list of top tags with counts
    """
    # For artists, use slightly higher limit since we're aggregating across more tracks
    aggregated = aggregate_tags_with_counts(track_list)
    top_tags = get_top_tags(aggregated, limit)
    return format_tags_for_display(top_tags)
