"""slskd download/search service.

This service owns slskd workflow logic:
- search retry/slot-busy behaviour
- result parsing
- quality filtering
- download tracking
- transfer parsing and cleanup

Raw HTTP is handled by api_clients.slskd_http.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass
from typing import Optional, Any

from api_clients.slskd_http import SlskdHttpClient

logger = logging.getLogger(__name__)

STUCK_SEARCH_TIMEOUT_MS = 3 * 60 * 1000
EMPTY_TERMINAL_STATES = frozenset({"Completed, Cancelled", "Completed, Errored", "Cancelled", "Errored"})


@dataclass
class SearchFile:
    filename: str
    size: int
    bitrate: int
    sample_rate: int
    length: int
    bit_depth: int | None = None
    extension: str | None = None
    is_locked: bool = False

    def __post_init__(self):
        self.size = int(self.size or 0)
        self.bitrate = int(self.bitrate or 0)
        self.sample_rate = int(self.sample_rate or 0)
        self.length = int(self.length or 0)
        if self.bit_depth is not None:
            self.bit_depth = int(self.bit_depth) if self.bit_depth else None

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024) if self.size else 0

    @property
    def duration_formatted(self) -> str:
        if not self.length:
            return "0:00"
        return f"{self.length // 60}:{self.length % 60:02d}"

    @property
    def is_lossless(self) -> bool:
        return (self.extension or "").lower() in ("flac", "wav", "aiff", "alac")

    def matches_quality(self, min_bitrate: int = 192, min_sample_rate: int = 44100) -> bool:
        """Check if this file meets minimum quality thresholds.

        Lossless formats (FLAC, WAV, etc.) pass automatically on bitrate
        since their bitrate varies with content.  Sample rate must still
        meet the minimum.
        """
        if self.is_locked:
            return False
        if self.is_lossless:
            return self.sample_rate >= min_sample_rate
        return self.bitrate >= min_bitrate and self.sample_rate >= min_sample_rate


@dataclass
class SearchResponse:
    username: str
    files: list[SearchFile]
    has_free_upload_slot: bool = True
    upload_speed: int | None = None
    queue_length: int | None = None

    def __post_init__(self):
        if not self.files:
            self.files = []
        elif isinstance(self.files[0], dict):
            self.files = [
                SearchFile(
                    filename=f.get("filename", ""),
                    size=f.get("size", 0),
                    bitrate=f.get("bitRate", 0),
                    sample_rate=f.get("sampleRate", 0),
                    length=f.get("length", 0),
                    bit_depth=f.get("bitDepth"),
                    extension=f.get("extension"),
                    is_locked=bool(f.get("isLocked", f.get("is_locked", False))),
                )
                for f in self.files
            ]


class SlskdService:
    """Application-level slskd behaviour."""

    STATE_REQUESTED = "Requested"
    STATE_QUEUED_REMOTELY = "Queued, Remotely"
    STATE_QUEUED_LOCALLY = "Queued, Locally"
    STATE_INITIALIZING = "Initializing"
    STATE_IN_PROGRESS = "InProgress"
    STATE_SUCCEEDED = "Completed, Succeeded"
    STATE_CANCELLED = "Completed, Cancelled"
    STATE_TIMED_OUT = "Completed, TimedOut"
    STATE_ERRORED = "Completed, Errored"
    STATE_REJECTED = "Completed, Rejected"

    ACTIVE_STATES = frozenset([
        STATE_REQUESTED, STATE_QUEUED_REMOTELY, STATE_QUEUED_LOCALLY,
        STATE_INITIALIZING, STATE_IN_PROGRESS, "Queued", "In Progress", "Downloading",
    ])
    FAILED_STATES = frozenset([
        STATE_CANCELLED, STATE_TIMED_OUT, STATE_ERRORED, STATE_REJECTED,
        "Cancelled", "TimedOut", "Errored", "Failed", "Rejected", "Error",
    ])

    def __init__(self, http_client: SlskdHttpClient):
        self.http = http_client

    @staticmethod
    def state_text(raw_state) -> str:
        if raw_state is None:
            return ""
        if isinstance(raw_state, dict):
            raw_state = raw_state.get("state") or raw_state.get("name") or raw_state.get("value")
        return str(raw_state).strip()

    @classmethod
    def is_success_state(cls, raw_state) -> bool:
        state = cls.state_text(raw_state)
        state_lower = state.lower()
        return bool(state == cls.STATE_SUCCEEDED or "succeed" in state_lower or state_lower in {"completed", "complete", "succeeded"})

    @staticmethod
    def extract_queue_position(entry: dict | None) -> Optional[int]:
        if not isinstance(entry, dict):
            return None
        raw = entry.get("queuePosition") or entry.get("queue_position") or entry.get("position") or entry.get("queueIndex") or entry.get("queueLength")
        try:
            return int(raw) if raw is not None else None
        except Exception:
            return None

    def start_search(self, query: str, timeout: Optional[int] = None, max_attempts: int = 5) -> Optional[str]:
        if not self.http.enabled:
            return None
        data = {"searchText": query, "filterResponses": False}
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.http.post_json("searches", data, timeout=timeout)
                if resp.status_code in [200, 201]:
                    payload = resp.json() or {}
                    return payload.get("id") or payload.get("searchId")
                body_preview = (resp.text or "")[:200]
                retryable_429 = resp.status_code == 429 and (
                    "only one concurrent operation" in body_preview.lower()
                    or "wait until the previous request completes" in body_preview.lower()
                )
                if retryable_429 and attempt < max_attempts:
                    wait_seconds = 0.8
                    retry_after = (resp.headers.get("Retry-After") or "").strip()
                    if retry_after:
                        try:
                            wait_seconds = max(0.2, float(retry_after))
                        except Exception:
                            pass
                    else:
                        wait_seconds = min(2.0, 0.4 * attempt)
                    time.sleep(wait_seconds)
                    continue
                logger.warning("slskd search start failed: %s - %s", resp.status_code, body_preview)
                return None
            except Exception as exc:
                # Include the exception class (ConnectTimeout vs ReadTimeout
                # vs ConnectionRefused) and the resolved API URL so a failed
                # search is self-diagnosing: ConnectTimeout = wrong/firewalled
                # URL, ReadTimeout = slskd reachable but not answering search
                # requests (usually its Soulseek connection is down).
                logger.error(
                    "slskd search failed for query %r: %s (%s) [%s]",
                    query,
                    exc,
                    type(exc).__name__,
                    getattr(self.http, "base_url", "?"),
                    exc_info=True,
                )
                return None
        return None

    def get_search_results(self, search_id: str, timeout: Optional[int] = None) -> tuple[list[SearchResponse], str, bool]:
        if not self.http.enabled:
            return [], "Error", True
        state = "InProgress"
        try:
            state_data = self.http.get_json(f"searches/{search_id}", timeout=timeout, default={})
            state = state_data.get("state", "InProgress")
        except Exception as exc:
            logger.error("slskd get state failed for search %s: %s", search_id, exc, exc_info=True)
            return [], state, False
        if state in EMPTY_TERMINAL_STATES:
            return [], state, True
        try:
            raw = self.http.get_json(f"searches/{search_id}/responses", timeout=timeout, default=[])
            responses = []
            for raw_resp in raw or []:
                if isinstance(raw_resp, dict):
                    responses.append(SearchResponse(
                        username=raw_resp.get("username", "Unknown"),
                        files=raw_resp.get("files", []),
                        has_free_upload_slot=raw_resp.get("hasFreeUploadSlot", raw_resp.get("HasFreeUploadSlot", True)),
                        upload_speed=raw_resp.get("uploadSpeed"),
                        queue_length=raw_resp.get("queueLength"),
                    ))
            return responses, state, state not in self.ACTIVE_STATES
        except Exception as exc:
            logger.warning("slskd get responses failed for search %s: %s", search_id, exc)
            return [], state, state not in self.ACTIVE_STATES

    def download_file(self, username: str, filename: str, size: int = 0, timeout: Optional[int] = None) -> bool:
        if not self.http.enabled:
            return False
        try:
            resp = self.http.post_json(f"transfers/downloads/{username}", [{"filename": filename, "size": int(size or 0)}], timeout=timeout)
            return resp.status_code in [200, 201, 204]
        except Exception as exc:
            logger.error("slskd download exception for %s/%s: %s", username, filename[:50], exc, exc_info=True)
            return False

    def filter_results_by_quality(self, responses: list[SearchResponse], min_bitrate: int = 192, min_sample_rate: int = 44100, max_results: int = 50) -> list[dict]:
        qualified = []
        for response in responses:
            for file in response.files:
                if file.matches_quality(min_bitrate, min_sample_rate):
                    qualified.append({
                        "username": response.username,
                        "filename": file.filename,
                        "size_mb": file.size_mb,
                        "bitrate": file.bitrate,
                        "sample_rate": file.sample_rate,
                        "bit_depth": file.bit_depth,
                        "extension": file.extension,
                        "is_lossless": file.is_lossless,
                        "duration": file.duration_formatted,
                        "length_seconds": file.length,
                        "has_free_upload_slot": getattr(response, "has_free_upload_slot", True),
                        "upload_speed": getattr(response, "upload_speed", None),
                        "queue_length": getattr(response, "queue_length", None),
                    })
        qualified.sort(key=lambda item: (-item["bitrate"], -item["sample_rate"]))
        return qualified[:max_results]

    def search_and_filter(self, query: str, min_bitrate: int | None = None, wait_seconds: int = 5, poll_interval: float = 1.0, timeout: Optional[int] = None) -> list[dict]:
        if min_bitrate is None:
            from helpers.config_helpers import get_search_quality_config
            min_bitrate = get_search_quality_config()["min_bitrate"]
        search_id = self.start_search(query, timeout=timeout)
        if not search_id:
            return []
        start = time.time()
        while time.time() - start < wait_seconds:
            responses, _state, is_complete = self.get_search_results(search_id, timeout=timeout)
            qualified = self.filter_results_by_quality(responses, min_bitrate=min_bitrate)
            if qualified:
                return qualified
            if is_complete:
                break
            time.sleep(poll_interval)
        responses, _state, _complete = self.get_search_results(search_id, timeout=timeout)
        return self.filter_results_by_quality(responses, min_bitrate=min_bitrate)

    def parse_transfers_response(self, raw: list | dict) -> list[dict]:
        if isinstance(raw, dict):
            raw = raw.get("downloads") or raw.get("transfers") or raw.get("items") or []
        flat = []
        for user_entry in raw or []:
            if not isinstance(user_entry, dict):
                continue
            username = user_entry.get("username", "Unknown")
            entries = []
            if user_entry.get("filename") and not user_entry.get("directories"):
                entries.append(user_entry)
            for directory in user_entry.get("directories") or []:
                entries.extend(directory.get("files") or directory.get("downloads") or [])
            entries.extend(user_entry.get("files") or user_entry.get("downloads") or [])
            for item in entries:
                if not isinstance(item, dict):
                    continue
                size = int(item.get("size", 0) or 0)
                bytes_transferred = int(item.get("bytesTransferred", 0) or 0)
                progress = min(100, round((bytes_transferred / size) * 100, 2)) if size else int(item.get("percentComplete", 0) or 0)
                flat.append({
                    "id": item.get("id") or item.get("remoteToken") or item.get("token") or "",
                    "username": username,
                    "filename": item.get("filename") or item.get("fileName") or item.get("name") or item.get("path") or "",
                    "size": size,
                    "bytesTransferred": bytes_transferred,
                    "progress": progress,
                    "state": self.state_text(item.get("state") or item.get("transferState") or item.get("status")),
                    "averageSpeed": int(item.get("averageSpeed", 0) or 0),
                    "queuePosition": self.extract_queue_position(item),
                    "localFilePath": item.get("localFilePath") or item.get("localPath") or item.get("downloadedFilePath") or item.get("path") or "",
                })
        return flat

    def get_active_downloads(self, timeout: Optional[int] = None) -> list[dict]:
        try:
            raw = self.http.get_json("transfers/downloads", timeout=timeout, default=[])
            return self.parse_transfers_response(raw)
        except Exception as exc:
            logger.error("slskd get active downloads failed: %s", exc, exc_info=True)
            return []

    def get_completed_transfers(self, timeout: Optional[int] = None) -> list[dict]:
        return [item for item in self.get_active_downloads(timeout=timeout) if self.is_success_state(item.get("state"))]

    def list_searches(self, timeout: Optional[int] = None) -> list[dict]:
        try:
            payload = self.http.get_json("searches", timeout=timeout, default=[])
            return payload if isinstance(payload, list) else []
        except Exception:
            return []

    def cancel_search(self, search_id: str, timeout: Optional[int] = None) -> bool:
        try:
            resp = self.http.delete(f"searches/{search_id}", timeout=timeout)
            if resp.status_code in [200, 204]:
                return True
            body = (resp.text or "")[:400]
            return resp.status_code in [409, 500] and "concurrency" in body.lower()
        except Exception:
            return False

    def cancel_download(self, username: str, transfer_id: str, remove: bool = True, timeout: Optional[int] = None) -> bool:
        try:
            resp = self.http.delete(f"transfers/downloads/{username}/{transfer_id}?remove={str(remove).lower()}", timeout=timeout)
            return resp.status_code in [200, 204]
        except Exception:
            return False

    def clear_completed_downloads(self, timeout: Optional[int] = None) -> bool:
        try:
            resp = self.http.delete("transfers/downloads/all/completed", timeout=timeout)
            return resp.status_code in [200, 204]
        except Exception:
            return False
