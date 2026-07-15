"""Simple Wikipedia API client.

Fetches content from Wikipedia's REST API for parsing upcoming releases
and other wiki-based data.
"""

from __future__ import annotations

import logging
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)


class WikipediaClient:
    """Wikipedia API wrapper."""

    def __init__(self, http_session=None, enabled: bool = True):
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://en.wikipedia.org/w/api.php"
        self.headers = {
            "User-Agent": "Popularr/2.0 (https://github.com/M0VENTURA/Popularr)",
            "Accept": "application/json",
        }

    def parse_page(self, page_title: str, prop: str = "text") -> dict[str, Any]:
        """Fetch parsed content from a Wikipedia page.
        
        Args:
            page_title: Wikipedia page title (e.g., "List_of_upcoming_albums")
            prop: Properties to fetch (default: "text")
            
        Returns:
            Parsed page data or empty dict on error
        """
        if not self.enabled:
            return {}
        
        params = {
            "action": "parse",
            "page": page_title,
            "format": "json",
            "prop": prop,
            "redirects": 1,
        }
        
        try:
            response = self.session.get(
                self.base_url,
                params=params,
                headers=self.headers,
                timeout=15,
            )

            # Check content-type before calling .json() to catch HTML error pages
            ct = (response.headers.get("content-type") or "").lower()
            if "text/html" in ct or "text/plain" in ct:
                logger.warning(
                    "Wikipedia API returned %s (HTTP %s) for page %s — possibly rate-limited",
                    ct, response.status_code, page_title,
                )
                return {}

            response.raise_for_status()
            data = response.json()

            # Check for Wikipedia API-level errors
            if "error" in data:
                error_info = data["error"].get("info", str(data["error"]))
                logger.warning("Wikipedia API error for '%s': %s", page_title, error_info)
                return {}

            return data.get("parse", {})
        except Exception as exc:
            logger.error("Failed to fetch Wikipedia page '%s': %s", page_title, exc)
            return {}
