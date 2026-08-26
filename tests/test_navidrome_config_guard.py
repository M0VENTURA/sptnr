"""Regression: Navidrome config with EMPTY base_url/user/pass must be treated
as not-configured.

The import scan built a NavidromeClient from a stub config row (empty
fields), producing requests to ``/rest/...`` (no host) that failed with
``unknown url type`` — so the scan imported 0 artists and the ``tracks``
table stayed permanently bare (which caused the whole album_artist saga).
"""

from __future__ import annotations

import pytest


class TestNavidromeFirstUserRejectsEmpty:
    def test_empty_user_returns_empty(self, monkeypatch):
        from helpers import config_helpers as ch

        monkeypatch.setattr(
            ch, "get_navidrome_users_normalized",
            lambda: [{"base_url": "", "user": "", "pass": ""}],
        )
        result = ch.get_navidrome_first_user()
        assert result == {}

    def test_partial_user_returns_empty(self, monkeypatch):
        from helpers import config_helpers as ch

        monkeypatch.setattr(
            ch, "get_navidrome_users_normalized",
            lambda: [{"base_url": "http://nav:4533", "user": "a", "pass": ""}],
        )
        assert ch.get_navidrome_first_user() == {}

    def test_valid_user_returned(self, monkeypatch):
        from helpers import config_helpers as ch

        good = {"base_url": "http://nav:4533", "user": "admin", "pass": "secret"}
        monkeypatch.setattr(
            ch, "get_navidrome_users_normalized",
            lambda: [good],
        )
        assert ch.get_navidrome_first_user() == good

    def test_stub_first_valid_second(self, monkeypatch):
        """A stub empty row first + a valid row second still resolves."""
        from helpers import config_helpers as ch

        good = {"base_url": "http://nav:4533", "user": "admin", "pass": "secret"}
        monkeypatch.setattr(
            ch, "get_navidrome_users_normalized",
            lambda: [{"base_url": "", "user": "", "pass": ""}, good],
        )
        assert ch.get_navidrome_first_user() == good

    def test_env_fallback_still_works(self, monkeypatch):
        from helpers import config_helpers as ch

        monkeypatch.setattr(ch, "get_navidrome_users_normalized", lambda: [])
        monkeypatch.setenv("NAV_BASE_URL", "http://nav:4533")
        monkeypatch.setenv("NAV_USER", "admin")
        monkeypatch.setenv("NAV_PASS", "secret")
        result = ch.get_navidrome_first_user()
        assert result["base_url"] == "http://nav:4533"


class TestNavidromeClientEmptyBaseUrlGuard:
    def test_raises_clear_error(self):
        from api_clients.navidrome import NavidromeClient

        client = NavidromeClient(base_url="", username="", password="")
        with pytest.raises(ValueError, match="base_url is empty"):
            client._get_subsonic_response("ping")

    def test_valid_base_url_no_guard_error(self, monkeypatch):
        from api_clients.navidrome import NavidromeClient

        client = NavidromeClient(base_url="http://nav:4533", username="u", password="p")
        # The guard passes; a connection failure is handled internally (the
        # client returns {} after retries) — NOT the empty-URL ValueError.
        monkeypatch.setattr(client.session, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("conn refused")))
        result = client._get_subsonic_response("ping")
        assert result == {}
