"""Cross-provider API rate limiter.

This manages thread-safe API request throttling and state tracking
across external providers (MusicBrainz, ListenBrainz, Last.fm, Spotify).

FIX (see notes below `_save_state`): state persistence is now performed
OUTSIDE each provider lock. Previously `_save_state()` (a synchronous
`json.dump()` to disk) ran INSIDE `self._mb_lock` / `self._lastfm_lock` /
`self._listenbrainz_lock`. Since every thread doing MusicBrainz/Last.fm/
ListenBrainz work funnels through the same provider-wide lock, a single
slow disk write (contended disk, degraded/network-mounted state file
path, etc.) would stall EVERY concurrent caller of that provider for the
duration of the write - not just the caller that happened to trigger the
write. This is consistent with reports of multiple, seemingly unrelated
"budget exceeded" scan-stage timeouts (e.g. album enrichment AND a
separate ListenBrainz album-tracklist lookup, both of which resolve
MusicBrainz release/artist data under the hood) surfacing together.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict

import structlog

logger = structlog.get_logger(__name__)

SPOTIFY_RATE_LIMIT_PER_30S = 250
SPOTIFY_DAILY_LIMIT = 500000
LASTFM_RATE_LIMIT_PER_SECOND = 1
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
        # Guards `_last_save_time` and the (rare) concurrent-write race on
        # `state_file` itself. Deliberately SEPARATE from the per-provider
        # throttle locks below, so a slow disk write can never block a
        # provider's request pacing.
        self._save_lock = threading.Lock()
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
        """Persist rate-limiter state to disk.

        IMPORTANT: this method performs synchronous file I/O and must
        NEVER be called while holding a per-provider throttle lock
        (`_mb_lock` / `_lastfm_lock` / `_listenbrainz_lock`). Doing so
        would let a single slow write stall every other thread waiting
        on that provider's lock, regardless of which thread's request
        actually triggered the write. It uses its own dedicated
        `_save_lock` (held only for the throttle-check + write, not for
        any provider-specific pacing).
        """
        with self._save_lock:
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
        # Compute the wait UNDER the lock (atomic claim of the next slot),
        # then sleep OUTSIDE it: concurrent scan workers (4 per album) must
        # sleep in parallel instead of serialising on the lock — a worker
        # holding the lock while sleeping turns a 1 req/s budget into "each
        # worker waits for every other worker's sleep", which is exactly the
        # "N of N futures unfinished" album stall.
        with self._mb_lock:
            now = time.time()
            last_request = self.state.get("musicbrainz_last_request", 0.0)
            wait_time = MUSICBRAINZ_MIN_INTERVAL - (now - last_request)
            self.state["musicbrainz_last_request"] = time.time()
            self.state["musicbrainz_daily_count"] = self.state.get("musicbrainz_daily_count", 0) + 1
        # Persisted outside _mb_lock - see _save_state docstring.
        self._save_state()
        if wait_time > 0:
            time.sleep(wait_time)

    def throttle_lastfm(self) -> None:
        """Enforce a maximum of 1 Last.fm request per second across threads."""
        with self._lastfm_lock:
            now = time.time()
            last_request = self.state.get("lastfm_last_request", 0.0)
            wait_time = LASTFM_RATE_LIMIT_PER_SECOND - (now - last_request)
            self.state["lastfm_last_request"] = time.time()
            self.state["lastfm_daily_count"] = self.state.get("lastfm_daily_count", 0) + 1
        # Persisted outside _lastfm_lock - see _save_state docstring.
        self._save_state()
        if wait_time > 0:
            time.sleep(wait_time)

    def throttle_listenbrainz(self) -> None:
        """Enforce ListenBrainz pacing on its own rate budget."""
        with self._listenbrainz_lock:
            now = time.time()
            last_request = self.state.get("listenbrainz_last_request", 0.0)
            wait_time = LISTENBRAINZ_MIN_INTERVAL - (now - last_request)
            self.state["listenbrainz_last_request"] = time.time()
            self.state["listenbrainz_daily_count"] = self.state.get("listenbrainz_daily_count", 0) + 1
        # Persisted outside _listenbrainz_lock - see _save_state docstring.
        self._save_state()
        if wait_time > 0:
            time.sleep(wait_time)

    def wait_if_needed_lastfm(self, max_wait_seconds: float = 2.0) -> bool:
        now = time.time()
        wait_time = LASTFM_RATE_LIMIT_PER_SECOND - (now - self.state.get("lastfm_last_request", 0.0))
        if wait_time <= 0:
            self.state["lastfm_last_request"] = now
            self.state["lastfm_daily_count"] = self.state.get("lastfm_daily_count", 0) + 1
            self._save_state()
            return True
        if wait_time <= max_wait_seconds:
            time.sleep(wait_time + 0.1)
            self.state["lastfm_last_request"] = time.time()
            self.state["lastfm_daily_count"] = self.state.get("lastfm_daily_count", 0) + 1
            self._save_state()
            return True
        return False

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
