"""Artist biography enrichment service using Wikidata/Wikipedia.

This service provides artist biography lookup functionality by querying Wikidata
and Wikipedia. It implements intelligent entity disambiguation to identify the
correct artist when multiple results are returned.

Key Features:
    - Multi-candidate entity resolution with musician-specific filtering
    - Fallback to Wikipedia summaries when available
    - Graceful error handling for API failures
    
Usage:
    bio_service = ArtistBioService()
    biography = bio_service.get_artist_biography("Artist Name")
    
    # Or use the convenience function:
    from services.enrichment.artist_bio_service import get_artist_biography
    biography = get_artist_biography("Artist Name")
"""

from __future__ import annotations

import logging
from typing import Optional

from api_clients.wikidata_http import WikidataHttpClient
from helpers.config_helpers import get_musician_terms

logger = logging.getLogger(__name__)

# Musician-related occupation terms used for entity disambiguation.
# Loaded from centralized config (with defaults)
MUSICIAN_TERMS = get_musician_terms()


class ArtistBioService:
    """Service for retrieving artist biographies from Wikidata/Wikipedia.
    
    Attributes:
        http: Wikidata HTTP client instance for API calls.
    """
    
    def __init__(self, http_client: WikidataHttpClient | None = None):
        """Initialize the biography service.
        
        Args:
            http_client: Optional custom Wikidata HTTP client. If not provided,
                a default client will be created.
        """
        self.http = http_client or WikidataHttpClient()

    @staticmethod
    def pick_best_entity(artist_name: str, results: list) -> Optional[str]:
        """Select the most relevant Wikidata entity for an artist name.
        
        This method implements a three-pass disambiguation strategy:
        1. First pass: Look for entities described with musician-related terms
        2. Second pass: Exact name match (case-insensitive)
        3. Third pass: Partial name containment
        
        Args:
            artist_name: The artist name to find an entity for.
            results: List of Wikidata search results, each containing 'id',
                'label', and optionally 'description'.
                
        Returns:
            The Wikidata entity ID (e.g., 'Q12345') if a match is found,
            None otherwise.
        """
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
        
        # Pass 3: Partial containment (name in label or label in name)
        for result in results:
            label = result.get("label", "").lower()
            if artist_lower in label or label in artist_lower:
                return result.get("id")
        
        return None

    def get_artist_biography(self, artist_name: str) -> Optional[str]:
        """Retrieve the biography for a given artist.
        
        The method attempts to fetch a full Wikipedia summary first, falling
        back to the Wikidata entity description if no Wikipedia article exists.
        
        Args:
            artist_name: The name of the artist to look up.
            
        Returns:
            The artist biography text if found, None otherwise.
            
        Note:
            Returns None for empty input strings or if any API call fails.
            Failures are logged at DEBUG level to avoid noise.
        """
        if not artist_name or not artist_name.strip():
            return None
            
        try:
            # Single-word names (e.g. "Poppy", "Cher") are notoriously ambiguous
            # on Wikidata — a plain search surfaces the plant, films, people,
            # etc. before the musician.  Append a musician qualifier to the
            # search query for those names so the artist entity surfaces first,
            # falling back to the bare name when the qualified queries miss.
            queries: list[str] = []
            stripped = artist_name.strip()
            if stripped and " " not in stripped:
                queries = [f"{stripped} (singer)", f"{stripped} (musician)", stripped]
            else:
                queries = [artist_name]

            entity_id = None
            for query in queries:
                # Search for matching entities
                results = self.http.search_entities(query, limit=5)
                if not results:
                    continue
                # Disambiguate and select best entity
                entity_id = self.pick_best_entity(artist_name, results)
                if entity_id:
                    break
            if not entity_id:
                return None
                
            # Fetch full entity data
            entity = self.http.get_entity(entity_id)
            
            # Try to get Wikipedia summary first (more detailed)
            wiki_title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
            if wiki_title:
                bio = self.http.get_wikipedia_summary(wiki_title)
                if bio:
                    return bio
                    
            # Fallback to Wikidata description
            return entity.get("descriptions", {}).get("en", {}).get("value")
            
        except Exception as exc:
            logger.debug("Artist biography lookup failed for %s: %s", artist_name, exc)
            return None


def get_artist_biography(artist_name: str) -> Optional[str]:
    """Convenience function to retrieve an artist biography.
    
    This is a shorthand for creating an ArtistBioService instance and calling
    get_artist_biography() on it. Suitable for one-off lookups.
    
    Args:
        artist_name: The name of the artist to look up.
        
    Returns:
        The artist biography text if found, None otherwise.
        
    Example:
        >>> from services.enrichment.artist_bio_service import get_artist_biography
        >>> bio = get_artist_biography("The Beatles")
        >>> print(bio[:100])  # First 100 characters
        'The Beatles were an English rock band formed in Liverpool in 1960...'
    """
    return ArtistBioService().get_artist_biography(artist_name)
