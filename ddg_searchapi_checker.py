#!/usr/bin/env python3
"""
DuckDuckGo SearchAPI.io integration for official video detection.
Last-resort check when Discogs/MusicBrainz video detection insufficient.
"""

import os
import re
import json
import unicodedata
import logging
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.searchapi.io/api/v1/search"
SEARCHAPI_KEY = os.getenv("SEARCHAPI_IO_KEY")

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/118.0 Safari/537.36"
}
REQ_TIMEOUT = 12


def _norm(s: str) -> str:
    """Normalize string for comparison."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.lower().strip()
    s = s.replace("'", "'")
    s = re.sub(r"\s+", " ", s)
    return s


def _has_official_video_terms(title: str) -> bool:
    """Check if title contains 'official' and 'video'."""
    t = _norm(title)
    return ("official" in t) and ("video" in t)


def _contains_artist_track(title: str, artist: str, track: str) -> bool:
    """Check if title contains both artist and track terms."""
    t = _norm(title)
    return (_norm(artist) in t) and (_norm(track) in t)


def _is_label_match(channel: str, labels: List[str]) -> bool:
    """Check if channel matches any whitelisted label."""
    ch = _norm(channel)
    for lbl in labels:
        if _norm(lbl) in ch or ch in _norm(lbl):
            return True
    return False


def _is_artist_match(channel: str, artist: str) -> bool:
    """Check if channel matches artist (official or Topic)."""
    ch, art = _norm(channel), _norm(artist)
    if ch == art or art in ch or ch in art:
        return True
    if art in ch and ("official" in ch or "topic" in ch):
        return True
    return False


def _get_youtube_channel_from_page(video_url: str) -> Optional[str]:
    """Load the YouTube page and extract the channel name via JSON-LD or header link."""
    try:
        r = requests.get(video_url, headers=HTTP_HEADERS, timeout=REQ_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logger.debug(f"Failed to fetch YouTube page {video_url}: {e}")
        return None

    try:
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        logger.debug(f"Failed to parse YouTube page: {e}")
        return None

    # Prefer JSON-LD publisher/author
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for obj in objs:
            author = obj.get("author")
            if isinstance(author, dict) and author.get("name"):
                return author["name"]
            if isinstance(author, list):
                for a in author:
                    if isinstance(a, dict) and a.get("name"):
                        return a["name"]
            publisher = obj.get("publisher")
            if isinstance(publisher, dict) and publisher.get("name"):
                return publisher["name"]

    # Fallback: author link in header
    ch_el = soup.select_one("[itemprop='author'] a")
    if ch_el and ch_el.get_text(strip=True):
        return ch_el.get_text(strip=True)

    return None


def ddg_searchapi_official_video_match(
    artist: str,
    track_title: str,
    label_whitelist: List[str],
    exception_artists: Optional[List[str]] = None,
    locale: str = "us-en",
    safe: str = "moderate",
    time_period: str = "any_time"
) -> Dict:
    """
    Last-resort check via SearchAPI.io DuckDuckGo:
      - Query: site:youtube.com "artist" "track_title"
      - Enforce: title has artist+track AND contains 'Official' and 'Video'
      - Channel must match artist OR label whitelist

    Returns:
      { matched, reason, channel, video_title, video_url, weight }
    """
    exception_artists = exception_artists or []
    
    if not SEARCHAPI_KEY:
        logger.debug("SEARCHAPI_IO_KEY not set, skipping DDG video search")
        return {
            "matched": False, "reason": "SEARCHAPI_IO_KEY missing",
            "channel": None, "video_title": None, "video_url": None, "weight": 0
        }

    params = {
        "engine": "duckduckgo",
        "q": f'site:youtube.com "{artist}" "{track_title}"',
        "locale": locale,
        "safe": safe,
        "time_period": time_period,
        "api_key": SEARCHAPI_KEY
    }

    try:
        resp = requests.get(SEARCH_URL, params=params, headers=HTTP_HEADERS, timeout=REQ_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug(f"SearchAPI.io error for {artist} - {track_title}: {e}")
        return {
            "matched": False, "reason": f"SearchAPI.io error: {e}",
            "channel": None, "video_title": None, "video_url": None, "weight": 0
        }

    # SearchAPI.io returns DuckDuckGo organic results; take top YouTube entry
    org = data.get("organic_results", [])
    top = next((r for r in org if "youtube.com" in (r.get("link") or "")), None)

    if not top:
        logger.debug(f"No YouTube result in top organic results for {artist} - {track_title}")
        return {
            "matched": False, "reason": "No YouTube result in top organic results",
            "channel": None, "video_title": None, "video_url": None, "weight": 0
        }

    video_title = top.get("title") or ""
    video_url = top.get("link") or ""

    if not _contains_artist_track(video_title, artist, track_title):
        logger.debug(f"Top title missing artist/track terms: {video_title}")
        return {
            "matched": False, "reason": "Top title missing artist/track terms",
            "channel": None, "video_title": video_title, "video_url": video_url, "weight": 0
        }

    if not _has_official_video_terms(video_title):
        logger.debug(f"Top title lacks 'Official' and 'Video': {video_title}")
        return {
            "matched": False, "reason": "Top title lacks 'Official' and 'Video'",
            "channel": None, "video_title": video_title, "video_url": video_url, "weight": 0
        }

    channel = _get_youtube_channel_from_page(video_url)
    if not channel:
        logger.debug(f"Unable to determine channel for {video_url}")
        return {
            "matched": False, "reason": "Unable to determine channel",
            "channel": None, "video_title": video_title, "video_url": video_url, "weight": 0
        }

    is_label = _is_label_match(channel, label_whitelist)
    is_artist = _is_artist_match(channel, artist)
    matched = bool(is_label or is_artist)

    # Weighting: keep low; lower further for exception artists
    weight = 20
    if _norm(artist) in [_norm(x) for x in exception_artists]:
        weight = 5

    reason = (
        "Channel matched label whitelist" if is_label else
        "Channel matched artist" if is_artist else
        "Channel did not match artist/labels"
    )

    logger.debug(f"DDG video check for {artist} - {track_title}: matched={matched}, channel={channel}")

    return {
        "matched": matched, "reason": reason,
        "channel": channel, "video_title": video_title, "video_url": video_url,
        "weight": weight
    }
