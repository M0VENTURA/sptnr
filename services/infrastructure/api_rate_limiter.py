"""Cross-provider API rate limiter.

This manages thread-safe API request throttling and state tracking
across external providers (MusicBrainz, ListenBrainz, Last.fm, Spotify).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

SPOTIFY_RATE_LIMIT_PER_30S = 250
SPOTIFY_DAILY_LIMIT = 500000
LASTFM_RATE_LIMIT_PER_SECOND = 1.0
LASTFM_DAILY_LIMIT = 50000
MUSICBRAINZ_MIN_INTERVAL = 1.0
LISTENBRAINZ_MIN_INTERVAL = 1.0
LISTENBRAINZ_DAILY_LIMIT = 50000


class APIRateLimiter:
    _STATE_SAVE_INTERVAL_SECONDS = 30

    def __init__(self, state_file: str | None = None) -> None:
        if state_file is None:
            from helpers.config_helpers import get_api_rate_limiter_state_file
            state_file = get_api_rate_limiter_state_file()
        self.state_file = state_file
        self.state = self._load_state()
        self._last_save_time = 0.0
        self._mb_lock = threading.Lock()
        self._lastfm_lock = threading.Lock()
        self._listenbrainz_lock = threading.Lock()

    def _load_state(self) -> dict[str, Any]:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                last_reset = state.get("last_reset", "")
                if last_reset and datetime.fromisoformat(last_reset).date() < datetime.now().date():
                    state["spotify_daily_count"] = 0
                    state["lastfm_daily_count"] = 0
                    state["musicbrainz_daily_count"] = 0
                    state["listenbrainz_daily_count"] = 0
                    state["last_reset"] = datetime.now().isoformat()
                    self.state = state
                    self._save_state(force=True)
                return state
            except Exception as exc:
                logger.debug("Could not load API rate limiter state", error=str(exc))
        return {
            "spotify_daily_count": 0,
            "lastfm_daily_count": 0,
            "musicbrainz_daily_count": 0,
            "listenbrainz_daily_count": 0,
            "spotify_recent_requests": [],
            "lastfm_last_request": 0.0,
            "musicbrainz_last_request": 0.0,
            "listenbrainz_last_request": 0.0,
            "last_reset": datetime.now().isoformat(),
        }

    def _save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save_time < self._STATE_SAVE_INTERVAL_SECONDS:
            return
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2)
            self._last_save_time = now
        except Exception as exc:
            logger.debug("Could not save API rate limiter state", error=str(exc))

    def throttle_musicbrainz(self) -> None:
        with self._mb_lock:
            now = time.time()
            last_request = self.state.get("musicbrainz_last_request", 0.0)
            
            # Project the next available time slot
            allowed_time = max(now, last_request + MUSICBRAINZ_MIN_INTERVAL)
            wait_time = allowed_time - now
            
            # Reserve this future time slot for the current thread
            self.state["musicbrainz_last_request"] = allowed_time
            self.state["musicbrainz_daily_count"] = self.state.get("musicbrainz_daily_count", 0) + 1
            self._save_state()
            
        if wait_time > 0:
            time.sleep(wait_time)

    def throttle_lastfm(self) -> None:
        with self._lastfm_lock:
            now = time.time()
            last_request = self.state.get("lastfm_last_request", 0.0)
            
            allowed_time = max(now, last_request + LASTFM_RATE_LIMIT_PER_SECOND)
            wait_time = allowed_time - now
            
            self.state["lastfm_last_request"] = allowed_time
            self.state["lastfm_daily_count"] = self.state.get("lastfm_daily_count", 0) + 1
            self._save_state()
            
        if wait_time > 0:
            time.sleep(wait_time)

    def throttle_listenbrainz(self) -> None:
        with self._listenbrainz_lock:
            now = time.time()
            last_request = self.state.get("listenbrainz_last_request", 0.0)
            
            allowed_time = max(now, last_request + LISTENBRAINZ_MIN_INTERVAL)
            wait_time = allowed_time - now
            
            self.state["listenbrainz_last_request"] = allowed_time
            self.state["listenbrainz_daily_count"] = self.state.get("listenbrainz_daily_count", 0) + 1
            self._save_state()
            
        if wait_time > 0:
            time.sleep(wait_time)

    def wait_if_needed_lastfm(self, max_wait_seconds: float = 2.0) -> bool:
        # Added missing lock to prevent state corruption across threads
        with self._lastfm_lock:
            now = time.time()
            last_request = self.state.get("lastfm_last_request", 0.0)
            
            allowed_time = max(now, last_request + LASTFM_RATE_LIMIT_PER_SECOND)
            wait_time = allowed_time - now
            
            if wait_time <= max_wait_seconds:
                self.state["lastfm_last_request"] = allowed_time
                self.state["lastfm_daily_count"] = self.state.get("lastfm_daily_count", 0) + 1
                self._save_state()
                should_wait = True
            else:
                should_wait = False
                wait_time = 0.0
                
        if should_wait and wait_time > 0:
            time.sleep(wait_time)
            return True
            
        return should_wait

    def get_stats(self) -> dict[str, Any]:
        now = time.time()
        recent_spotify = [ts for ts in self.state.get("spotify_recent_requests", []) if now - ts < 30]
        return {
            "spotify_daily_count": self.state.get("spotify_daily_count", 0),
            "spotify_daily_limit": SPOTIFY_DAILY_LIMIT,
            "spotify_recent_30s": len(recent_spotify),
            "spotify_30s_limit": SPOTIFY_RATE_LIMIT_PER_30S,
            "lastfm_daily_count": self.state.get("lastfm_daily_count", 0),
            "lastfm_daily_limit": LASTFM_DAILY_LIMIT,
            "musicbrainz_daily_count": self.state.get("musicbrainz_daily_count", 0),
            "listenbrainz_daily_count": self.state.get("listenbrainz_daily_count", 0),
            "listenbrainz_daily_limit": LISTENBRAINZ_DAILY_LIMIT,
            "last_reset": self.state.get("last_reset", ""),
        }


_rate_limiter: APIRateLimiter | None = None


def get_rate_limiter() -> APIRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = APIRateLimiter()
    return _rate_limiter
