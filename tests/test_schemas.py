"""Tests for Pydantic request schemas in routes/schemas.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from routes.schemas import ScanRequest, SearchQuery, PaginationParams


class TestScanRequest:
    def test_valid_modes(self):
        for mode in ("popularity", "singles", "metadata", "all"):
            s = ScanRequest(mode=mode)
            assert s.mode == mode

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            ScanRequest(mode="invalid_mode")

    def test_default_mode(self):
        s = ScanRequest()
        assert s.mode == "popularity"

    def test_force_default_false(self):
        s = ScanRequest()
        assert s.force is False

    def test_force_true(self):
        s = ScanRequest(force=True)
        assert s.force is True


class TestSearchQuery:
    def test_valid_query(self):
        s = SearchQuery(query="test")
        assert s.query == "test"

    def test_query_too_short(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="a")

    def test_limit_default(self):
        s = SearchQuery(query="test")
        assert s.limit == 20

    def test_limit_clamped(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="test", limit=200)


class TestPaginationParams:
    def test_defaults(self):
        p = PaginationParams()
        assert p.page == 1
        assert p.per_page == 20

    def test_valid_custom(self):
        p = PaginationParams(page=3, per_page=50)
        assert p.page == 3
        assert p.per_page == 50

    def test_page_zero_rejected(self):
        with pytest.raises(ValidationError):
            PaginationParams(page=0)
