"""Low-level Wikidata/Wikipedia HTTP client."""

from __future__ import annotations

from urllib.parse import quote

from api_clients.http_utils import create_retry_client

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"


class WikidataHttpClient:
    def __init__(self, http_session=None):
        self.session = http_session or create_retry_client(
            user_agent="Popularr/2.0 ( https://github.com/M0VENTURA/Popularr )",
            retries=2,
            backoff=1.0,
        )

    def search_entities(self, name: str, limit: int = 5) -> list[dict]:
        resp = self.session.get(WIKIDATA_API, params={"action": "wbsearchentities", "search": name, "language": "en", "format": "json", "type": "item", "limit": limit}, timeout=6)
        resp.raise_for_status()
        return resp.json().get("search", [])

    def get_entity(self, entity_id: str) -> dict:
        resp = self.session.get(WIKIDATA_API, params={"action": "wbgetentities", "ids": entity_id, "props": "sitelinks|descriptions", "sitefilter": "enwiki", "languages": "en", "format": "json"}, timeout=6)
        resp.raise_for_status()
        return resp.json().get("entities", {}).get(entity_id, {})

    def get_wikipedia_summary(self, title: str) -> str | None:
        resp = self.session.get(WIKIPEDIA_SUMMARY_API.format(title=quote(title, safe="")), timeout=6)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        extract = (resp.json().get("extract") or "").strip()
        return extract or None
