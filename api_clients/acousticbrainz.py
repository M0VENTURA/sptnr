"""AcousticBrainz API client."""

from __future__ import annotations

import logging
import os
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)


class AcousticBrainzClient:
    """Small wrapper for AcousticBrainz high-level mood data."""

    def __init__(self, base_url: str | None = None, timeout: int = 10, http_session=None):
        self.base_url = (base_url or os.environ.get("ACOUSTICBRAINZ_BASE_URL") or "https://acousticbrainz.org").rstrip("/")
        self.timeout = timeout
        self.session = http_session or session

    def _request_json(self, path: str) -> dict[str, Any] | None:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logger.debug("AcousticBrainz request failed for %s: %s", url, exc)
            return None

    def get_high_level(self, recording_mbid: str) -> dict[str, Any] | None:
        mbid = (recording_mbid or "").strip()
        if not mbid:
            return None
        for path in (f"/api/v1/{mbid}/high-level", f"/{mbid}/high-level"):
            payload = self._request_json(path)
            if payload:
                return payload
        return None

    def get_primary_mood(self, recording_mbid: str) -> dict[str, Any] | None:
        payload = self.get_high_level(recording_mbid)
        high_level = payload.get("highlevel") if isinstance(payload, dict) else None
        if not isinstance(high_level, dict):
            return None
        best = None
        best_prob = -1.0
        for key, value in high_level.items():
            if not key.startswith("mood_") or not isinstance(value, dict):
                continue
            label = value.get("value")
            if not label:
                continue
            prob = value.get("probability")
            if prob is None and isinstance(value.get("all"), dict):
                prob = value["all"].get(label, 0.0)
            try:
                prob_f = float(prob or 0.0)
            except (TypeError, ValueError):
                prob_f = 0.0
            if prob_f > best_prob:
                best_prob = prob_f
                best = {"mood": str(label).replace("_", " ").strip().title(), "confidence": round(prob_f, 4), "signal": key}
        return best
