"""Regression tests for slskd transient-timeout resilience.

Soulseek/slskd serialises API operations, so a busy instance can answer
search/download requests slower than the HTTP timeout.  A single
ReadTimeout used to fail the entire queue item with ``no_results`` /
``download_failed``; these tests pin the retry behaviour.
"""

from __future__ import annotations

import time

import pytest

from services.downloads.slskd_service import SlskdService, _is_transient_error


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------

class TestIsTransientError:
    def test_read_timeout_is_transient(self):
        assert _is_transient_error(TimeoutError("timed out")) is True

    def test_connection_error_is_transient(self):
        assert _is_transient_error(ConnectionError("refused")) is True

    def test_httpx_read_timeout_is_transient(self):
        try:
            import httpx
        except ImportError:
            pytest.skip("httpx not installed")
        assert _is_transient_error(httpx.ReadTimeout("timed out")) is True
        assert _is_transient_error(httpx.ConnectTimeout("timed out")) is True

    def test_non_transient_error_is_not_retried(self):
        assert _is_transient_error(ValueError("bad payload")) is False


# ---------------------------------------------------------------------------
# start_search retries transient errors
# ---------------------------------------------------------------------------

class _FakeHttp:
    base_url = "http://localhost:5030/api/v0"

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = 0
        self.failures_before_success = 0

    def post_json(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise TimeoutError("timed out (ReadTimeout)")
        resp = _FakeResp(200, {"id": "search-123"})
        return resp


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload

    @property
    def text(self):
        return ""


class TestStartSearchRetry:
    def test_retries_transient_timeout_then_succeeds(self):
        http = _FakeHttp()
        http.failures_before_success = 2
        svc = SlskdService(http)
        search_id = svc.start_search("Spice Girls - Holler", max_attempts=5)
        assert search_id == "search-123"
        assert http.calls == 3

    def test_gives_up_after_max_attempts(self):
        http = _FakeHttp()
        http.failures_before_success = 100
        svc = SlskdService(http)
        search_id = svc.start_search("Spice Girls - Holler", max_attempts=3)
        assert search_id is None
        assert http.calls == 3

    def test_returns_none_when_disabled(self):
        svc = SlskdService(_FakeHttp(enabled=False))
        assert svc.start_search("anything") is None


# ---------------------------------------------------------------------------
# download_file retries transient errors
# ---------------------------------------------------------------------------

class TestDownloadFileRetry:
    def test_retries_transient_timeout_then_succeeds(self):
        http = _FakeHttp()
        http.failures_before_success = 1
        svc = SlskdService(http)
        assert svc.download_file("hudsonk1992", "/Spice Girls/track.flac") is True
        assert http.calls == 2

    def test_returns_false_when_disabled(self):
        svc = SlskdService(_FakeHttp(enabled=False))
        assert svc.download_file("peer", "file.mp3") is False


# ---------------------------------------------------------------------------
# get_search_results treats a state timeout as still-in-progress
# ---------------------------------------------------------------------------

class _FakeStateHttp(_FakeHttp):
    def get_json(self, endpoint, **kwargs):
        raise TimeoutError("timed out (ReadTimeout)")


class TestGetSearchResultsResilience:
    def test_state_timeout_keeps_search_in_progress(self, caplog):
        import logging
        svc = SlskdService(_FakeStateHttp())
        with caplog.at_level(logging.WARNING, logger="services.downloads.slskd_service"):
            responses, state, is_complete = svc.get_search_results("search-1")
        assert responses == []
        assert is_complete is False
        assert "still in progress" not in caplog.text  # no misleading log
