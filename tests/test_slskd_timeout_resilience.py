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


# ---------------------------------------------------------------------------
# start_search recovery: a POST that times out but actually started the search
# ---------------------------------------------------------------------------

class _RecoveringHttp(_FakeHttp):
    """POST always fails; list_searches exposes an active search with the query."""

    def __init__(self, active_searches=None, enabled=True):
        super().__init__(enabled=enabled)
        self._active = active_searches or []

    def post_json(self, *args, **kwargs):
        self.calls += 1
        raise TimeoutError("timed out (ReadTimeout)")

    def get_json(self, endpoint, **kwargs):
        if endpoint == "searches":
            return self._active
        return {}

    def put(self, *args, **kwargs):
        resp = _FakeResp(204)
        return resp

    def delete(self, *args, **kwargs):
        resp = _FakeResp(204)
        return resp


def _active_search(search_id="search-abc", query="Spice Girls - Holler", state="InProgress"):
    from datetime import datetime, timedelta, timezone
    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    return {"id": search_id, "state": state, "searchText": query, "startedAt": started.isoformat()}


class TestStartSearchRecovery:
    def test_recovers_search_started_despite_post_failure(self):
        http = _RecoveringHttp(active_searches=[_active_search()])
        svc = SlskdService(http)
        search_id = svc.start_search("Spice Girls - Holler", max_attempts=2)
        assert search_id == "search-abc"

    def test_returns_none_when_no_matching_active_search(self):
        http = _RecoveringHttp(active_searches=[_active_search(search_id="other", query="Something Else")])
        svc = SlskdService(http)
        assert svc.start_search("Spice Girls - Holler", max_attempts=2) is None

    def test_find_active_search_by_query_matches_query_and_state(self):
        http = _RecoveringHttp(active_searches=[
            _active_search(search_id="old", query="Spice Girls - Holler", state="Completed"),
            _active_search(search_id="new", query="Spice Girls - Holler"),
        ])
        svc = SlskdService(http)
        found = svc.find_active_search_by_query("Spice Girls - Holler")
        assert found is not None
        assert found["id"] == "new"


class TestClearStaleSearches:
    def test_cancels_terminal_and_stuck_searches_but_not_fresh(self):
        cancelled = []

        class _CleaningHttp(_RecoveringHttp):
            def delete(self, endpoint, *args, **kwargs):
                seg = endpoint.split("/")[-1]
                cancelled.append(seg.split("?")[0])
                return _FakeResp(204)

            def put(self, endpoint, *args, **kwargs):
                return _FakeResp(204)

        http = _CleaningHttp(active_searches=[
            _active_search(search_id="terminal", state="Completed"),
            _active_search(search_id="stuck", state="InProgress", query="Old Query"),
            _active_search(search_id="fresh", state="InProgress"),
        ])
        svc = SlskdService(http)
        svc.clear_stale_searches(budget_seconds=8)
        # terminal (completed) search is cleaned via DELETE
        assert "terminal" in cancelled
        # the fresh InProgress search must NOT be cancelled
        assert "fresh" not in cancelled


# ---------------------------------------------------------------------------
# query sanitisation parity with old_system
# ---------------------------------------------------------------------------

class TestQuerySanitisation:
    def test_ampersand_and_html_entities_become_spaces(self):
        from services.downloads.download_pipeline_service import _sanitize_slskd_query
        assert _sanitize_slskd_query("AC&amp;DC - Thunderstruck") == "AC DC - Thunderstruck"
        assert _sanitize_slskd_query("R\\u0026B - Song") == "R B - Song"
        assert _sanitize_slskd_query("AC/DC") == "AC/DC"

    def test_aggressive_punctuation_strip(self):
        from services.downloads.download_pipeline_service import _strip_all_query_punctuation_for_slskd
        assert _strip_all_query_punctuation_for_slskd("Where's the Love") == "Wheres the Love"
        assert _strip_all_query_punctuation_for_slskd("Hello! World") == "Hello World"
        assert _strip_all_query_punctuation_for_slskd("AC/DC") == "ACDC"


# ---------------------------------------------------------------------------
# candidate_duration is actually used by _score_soulseek_candidate
# ---------------------------------------------------------------------------

class TestSoulseekCandidateDurationScoring:
    def test_duration_bonus_raises_score(self):
        from services.queue.queue_scoring import _score_soulseek_candidate
        item = {"artist": "Spice Girls", "title": "Holler", "duration": "240"}
        base = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=None
        )
        exact = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=240
        )
        assert exact > base

    def test_large_duration_mismatch_penalises(self):
        from services.queue.queue_scoring import _score_soulseek_candidate
        item = {"artist": "Spice Girls", "title": "Holler", "duration": "240"}
        close = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=242
        )
        far = _score_soulseek_candidate(
            "Spice Girls - Holler.flac", item, candidate_duration=400
        )
        assert far < close


# ---------------------------------------------------------------------------
# year-mismatch guard in the pipeline scorer
# ---------------------------------------------------------------------------

class TestPipelineYearGuard:
    def test_year_mismatch_rejects(self):
        from services.downloads.download_pipeline_service import _year_mismatch_rejects
        assert _year_mismatch_rejects("Artist - Album [2012]/track.mp3", "2024") is True
        assert _year_mismatch_rejects("Artist - Album [2024]/track.mp3", "2024") is False
        assert _year_mismatch_rejects("Artist - Album (2012)/track.mp3", "2011") is False
        assert _year_mismatch_rejects("Artist - Album [2012]/track.mp3", None) is False

    def test_scorer_rejects_wrong_year_candidate(self):
        from services.downloads.download_pipeline_service import _score_result
        good = _score_result(
            {"filename": "Spice Girls - Holler (2000).mp3", "bitrate": 320},
            "Spice Girls", "Holler", expected_year="2000",
        )
        bad = _score_result(
            {"filename": "Spice Girls - Holler (2018).mp3", "bitrate": 320},
            "Spice Girls", "Holler", expected_year="2000",
        )
        assert good > 0
        assert bad == 0.0

