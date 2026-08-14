"""Tests for the authentication gate (helpers/app_hooks.before_request).

Verifies:
  - First-run mode (Navidrome unconfigured): the setup wizard + its API
    endpoints are reachable WITHOUT a session; other pages redirect to the
    setup wizard; other APIs return 401.
  - Configured mode: unauthenticated page requests redirect to the login
    page, unauthenticated API calls return 401, and an authenticated
    session is allowed through.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def first_run(monkeypatch):
    """Force the app into first-run mode (Navidrome not configured)."""
    monkeypatch.setattr(
        "helpers.config_helpers.needs_setup",
        lambda *a, **kw: True,
    )


@pytest.fixture()
def configured(monkeypatch):
    """Force the app into configured mode (Navidrome present)."""
    monkeypatch.setattr(
        "helpers.config_helpers.needs_setup",
        lambda *a, **kw: False,
    )


# ---------------------------------------------------------------------------
# First-run: setup wizard reachable without a session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_run_setup_page_reachable_without_session(first_run, unauthed_client):
    """The setup wizard must be reachable pre-login during first run."""
    response = await unauthed_client.get("/setup")
    assert response.status_code == 200
    html = (await response.get_data()).decode("utf-8", "replace")
    assert "Welcome to Popularr" in html


@pytest.mark.asyncio
async def test_first_run_login_page_reachable(first_run, unauthed_client):
    """The login page must be reachable pre-login during first run."""
    response = await unauthed_client.get("/login")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_first_run_other_pages_redirect_to_setup(first_run, unauthed_client):
    """Any other page (e.g. /dashboard) redirects to the setup wizard."""
    response = await unauthed_client.get("/dashboard")
    assert response.status_code in (301, 302)
    assert "/setup" in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_first_run_api_returns_401(first_run, unauthed_client):
    """APIs other than the setup wizard's are locked down during first run."""
    response = await unauthed_client.get("/api/stats")
    assert response.status_code == 401
    data = await response.get_json()
    assert data.get("success") is False


@pytest.mark.asyncio
async def test_first_run_setup_api_reachable_without_session(first_run, unauthed_client):
    """The setup wizard's own API endpoints must work without a session."""
    response = await unauthed_client.post(
        "/api/setup/save-partial",
        json={"navidrome_users": [{"base_url": "http://nav:4533", "user": "admin", "pass": "x"}]},
    )
    # save-partial writes to /dev/null config (CONFIG_PATH) so it may fail to
    # persist; the important thing is it is NOT blocked by the auth gate (401).
    assert response.status_code != 401


@pytest.mark.asyncio
async def test_first_run_test_connection_reachable(first_run, unauthed_client):
    """The 'Test Connection' button used by the wizard must not be gated."""
    response = await unauthed_client.post(
        "/api/test-navidrome-connection",
        json={"base_url": "http://navidrome:4533", "username": "admin", "password": "x"},
    )
    assert response.status_code != 401


# ---------------------------------------------------------------------------
# Configured: login required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_configured_page_redirects_to_login(configured, unauthed_client):
    """Unauthenticated page requests redirect to /login when configured."""
    response = await unauthed_client.get("/dashboard")
    assert response.status_code in (301, 302)
    assert "/login" in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_configured_api_returns_401(configured, unauthed_client):
    """Unauthenticated API calls return 401 JSON when configured."""
    response = await unauthed_client.get("/api/stats")
    assert response.status_code == 401
    data = await response.get_json()
    assert data.get("error") == "Authentication required"


@pytest.mark.asyncio
async def test_configured_authed_client_allowed(configured, client):
    """An authenticated session passes through the gate."""
    response = await client.get("/api/beets/status")
    # beets status may 404/500 in the test env, but it must NOT be 401.
    assert response.status_code != 401


@pytest.mark.asyncio
async def test_configured_login_page_reachable(configured, unauthed_client):
    """The login page stays reachable even when configured."""
    response = await unauthed_client.get("/login")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_configured_static_reachable(configured, unauthed_client):
    """Static assets must never require a session."""
    response = await unauthed_client.get("/static/does-not-matter.css")
    # Not 401 — either 200 (if exists) or 404 (missing file), never gated.
    assert response.status_code in (200, 404)
