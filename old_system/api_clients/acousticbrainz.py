"""AcousticBrainz client helpers for mood extraction (MBID-first)."""

import logging
import os
from typing import Any, Dict, Optional

from api_clients import session

logger = logging.getLogger(__name__)


class AcousticBrainzClient:
    """Small API wrapper for AcousticBrainz high-level mood data."""

    def __init__(self, base_url: Optional[str] = None, timeout: int = 10):
        self.base_url = (base_url or os.environ.get("ACOUSTICBRAINZ_BASE_URL") or "https://acousticbrainz.org").rstrip("/")
        self.timeout = timeout

    def _request_json(self, path: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        try:
            response = session.get(url, timeout=self.timeout)
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logger.debug(f"AcousticBrainz request failed for {url}: {exc}")
            return None

    def get_high_level(self, recording_mbid: str) -> Optional[Dict[str, Any]]:
        """Fetch high-level acoustic attributes for a recording MBID."""
        if not recording_mbid:
            return None

        mbid = recording_mbid.strip()
        if not mbid:
            return None

        # AcousticBrainz deployments have used both of these URL shapes.
        candidates = [
            f"/api/v1/{mbid}/high-level",
            f"/{mbid}/high-level",
        ]

        for path in candidates:
            payload = self._request_json(path)
            if payload:
                return payload
        return None

    def get_primary_mood(self, recording_mbid: str) -> Optional[Dict[str, Any]]:
        """Return best mood label + confidence from AcousticBrainz high-level data."""
        payload = self.get_high_level(recording_mbid)
        if not payload:
            return None

        high_level = payload.get("highlevel")
        if not isinstance(high_level, dict):
            return None

        best = None
        best_prob = -1.0

        for key, value in high_level.items():
            if not key.startswith("mood_"):
                continue
            if not isinstance(value, dict):
                continue

            label = value.get("value")
            if not label:
                continue

            prob = value.get("probability")
            if prob is None:
                all_scores = value.get("all")
                if isinstance(all_scores, dict):
                    try:
                        prob = all_scores.get(label, 0.0)
                    except Exception:
                        prob = 0.0
            try:
                prob_f = float(prob) if prob is not None else 0.0
            except (TypeError, ValueError):
                prob_f = 0.0

            if prob_f > best_prob:
                best_prob = prob_f
                best = {
                    "mood": str(label).replace("_", " ").strip().title(),
                    "confidence": round(prob_f, 4),
                    "signal": key,
                }

        return best
