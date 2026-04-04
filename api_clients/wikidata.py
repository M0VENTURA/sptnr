#!/usr/bin/env python3
"""
Wikidata/Wikipedia artist biography client.

Provides a Wikidata-based biography fallback for artists that have no bio
stored in the local database.  The lookup uses the public Wikidata search API
to resolve an artist name to a Wikidata entity, follows the English Wikipedia
sitelink to fetch the Wikipedia REST summary (which contains a well-formatted
introductory paragraph), and falls back to the short Wikidata entity description
when no Wikipedia article exists.

No API key is required.
"""

import logging
from typing import Optional
from urllib.parse import quote

from helpers.helpers import create_retry_session

logger = logging.getLogger(__name__)

# Wikidata and Wikipedia public API endpoints
_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_WIKIPEDIA_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

# Wikidata description terms that indicate a person/group is a musician
_MUSICIAN_TERMS = frozenset([
    "singer", "musician", "band", "rapper", "composer", "songwriter",
    "guitarist", "drummer", "bassist", "pianist", "vocalist", "producer",
    "dj", "disc jockey", "recording artist", "musical group", "rock group",
    "pop group", "hip-hop", "hip hop", "jazz", "blues", "country artist",
    "folk singer", "opera", "conductor", "orchestra",
])


def get_artist_biography(artist_name: str) -> Optional[str]:
    """Fetch an artist biography from Wikidata / Wikipedia.

    Strategy:
    1. Search Wikidata for entities matching *artist_name*.
    2. Pick the best match: prefer an entity whose Wikidata description
       contains a music-related keyword; fall back to the first result
       whose label exactly matches the query.
    3. If the entity has an English Wikipedia sitelink, fetch the
       Wikipedia REST summary (clean, prose introduction paragraph).
    4. If there is no Wikipedia article, return the shorter Wikidata
       entity description.

    Results are *not* cached here — the caller (app.py) is responsible for
    persisting the result to the ``artists`` table so subsequent requests
    are served from the DB.

    Args:
        artist_name: Artist display name to look up.

    Returns:
        Biography text, or ``None`` if nothing useful was found.
    """
    if not artist_name or not artist_name.strip():
        return None

    try:
        session = create_retry_session(
            user_agent="sptnr/2.0 ( https://github.com/M0VENTURA/sptnr )",
            retries=2,
            backoff=1.0,
        )

        # --- Step 1: search Wikidata entities ---
        search_resp = session.get(
            _WIKIDATA_API,
            params={
                "action": "wbsearchentities",
                "search": artist_name,
                "language": "en",
                "format": "json",
                "type": "item",
                "limit": 5,
            },
            timeout=6,
        )
        search_resp.raise_for_status()
        search_results = search_resp.json().get("search", [])

        if not search_results:
            logger.debug("[WIKIDATA] No search results for: %s", artist_name)
            return None

        # --- Step 2: pick best entity ---
        entity_id = _pick_best_entity(artist_name, search_results)
        if not entity_id:
            logger.debug("[WIKIDATA] Could not identify a music entity for: %s", artist_name)
            return None

        logger.debug("[WIKIDATA] Resolved %s -> %s", artist_name, entity_id)

        # --- Step 3: resolve to Wikipedia sitelink ---
        sitelink_resp = session.get(
            _WIKIDATA_API,
            params={
                "action": "wbgetentities",
                "ids": entity_id,
                "props": "sitelinks|descriptions",
                "sitefilter": "enwiki",
                "languages": "en",
                "format": "json",
            },
            timeout=6,
        )
        sitelink_resp.raise_for_status()
        entity_data = sitelink_resp.json().get("entities", {}).get(entity_id, {})

        wiki_title = (
            entity_data.get("sitelinks", {}).get("enwiki", {}).get("title")
        )

        # --- Step 4a: Wikipedia REST summary (preferred) ---
        if wiki_title:
            bio = _fetch_wikipedia_summary(session, wiki_title)
            if bio:
                logger.info(
                    "[WIKIDATA] Found Wikipedia bio for %s (%d chars)", artist_name, len(bio)
                )
                return bio

        # --- Step 4b: Wikidata short description (fallback) ---
        wikidata_desc = (
            entity_data.get("descriptions", {}).get("en", {}).get("value")
        )
        if wikidata_desc:
            logger.info(
                "[WIKIDATA] Using Wikidata description for %s: %s",
                artist_name,
                wikidata_desc,
            )
            return wikidata_desc

        return None

    except Exception as exc:
        logger.debug("[WIKIDATA] Error fetching biography for %s: %s", artist_name, exc)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pick_best_entity(artist_name: str, results: list) -> Optional[str]:
    """Return the Wikidata QID that best matches *artist_name* as a musician."""
    artist_lower = artist_name.strip().lower()

    # First pass: prefer entities whose description contains a music keyword
    for result in results:
        desc = result.get("description", "").lower()
        if any(term in desc for term in _MUSICIAN_TERMS):
            return result.get("id")

    # Second pass: accept any result whose label exactly matches the query
    for result in results:
        if result.get("label", "").lower() == artist_lower:
            return result.get("id")

    # Third pass: accept the first result if labels are similar (substring)
    for result in results:
        label = result.get("label", "").lower()
        if artist_lower in label or label in artist_lower:
            return result.get("id")

    return None


def _fetch_wikipedia_summary(session, title: str) -> Optional[str]:
    """Fetch the introductory extract from the Wikipedia REST summary API."""
    try:
        url = _WIKIPEDIA_SUMMARY_API.format(title=quote(title, safe=""))
        resp = session.get(url, timeout=6)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        extract = (data.get("extract") or "").strip()
        return extract if extract else None
    except Exception as exc:
        logger.debug("[WIKIDATA] Wikipedia summary fetch failed for %s: %s", title, exc)
        return None
