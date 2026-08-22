"""Artist biography enrichment service using Wikidata/Wikipedia.

This service provides artist biography lookup functionality by querying Wikidata
and Wikipedia. It implements intelligent entity disambiguation to identify the
correct artist when multiple results are returned.

Key Features:
    - Multi-candidate entity resolution with musician-specific filtering
    - Fallback to Wikipedia summaries when available
    - Graceful error handling for API failures
"""

from __future__ import annotations

from typing import Any, Optional

import structlog

from api_clients.wikidata_http import WikidataHttpClient
from helpers.config_helpers import get_musician_terms

logger = structlog.get_logger(__name__)

MUSICIAN_TERMS = get_musician_terms()


class ArtistBioService:
    """Service for retrieving artist biographies from Wikidata/Wikipedia."""

    def __init__(self, http_client: WikidataHttpClient | None = None):
        self.http = http_client or WikidataHttpClient()

    @staticmethod
    def pick_best_entity(artist_name: str, results: list[dict[str, Any]]) -> Optional[str]:
        """Select the most relevant Wikidata entity for an artist name."""
        artist_lower = artist_name.strip().lower()

        # Pass 1: Prioritize entities described as musicians
        for result in results:
            desc = result.get("description", "").lower()
            if any(term in desc for term in MUSICIAN_TERMS):
                return result.get("id")

        # Pass 2: Exact label match
        for result in results:
            if result.get("label", "").lower() == artist_lower:
                return result.get("id")

        # Pass 3: Partial containment
        for result in results:
            label = result.get("label", "").lower()
            if artist_lower in label or label in artist_lower:
                return result.get("id")

        return None

    def get_artist_biography(self, artist_name: str) -> Optional[str]:
        """Retrieve the biography for a given artist."""
        if not artist_name or not artist_name.strip():
            return None

        try:
            queries: list[str] = []
            stripped = artist_name.strip()
            if stripped and " " not in stripped:
                queries = [f"{stripped} (singer)", f"{stripped} (musician)", stripped]
            else:
                queries = [artist_name]

            entity_id = None
            for query in queries:
                results = self.http.search_entities(query, limit=5)
                if not results:
                    continue
                entity_id = self.pick_best_entity(artist_name, results)
                if entity_id:
                    break

            if not entity_id:
                return None

            entity = self.http.get_entity(entity_id)

            wiki_title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
            if wiki_title:
                bio = self.http.get_wikipedia_summary(wiki_title)
                if bio:
                    return bio

            return entity.get("descriptions", {}).get("en", {}).get("value")

        except Exception as exc:
            logger.debug("Artist biography lookup failed", artist=artist_name, error=str(exc))
            return None


def get_artist_biography(artist_name: str) -> Optional[str]:
    """Convenience function to retrieve an artist biography."""
    return ArtistBioService().get_artist_biography(artist_name)
