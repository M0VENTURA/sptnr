"""Tests for security/consistency fixes:

1. Login no longer falls back to comparing the submitted password against the
   stored plaintext Navidrome password in config.yaml — credentials must be
   verified LIVE against Navidrome (audit finding #2).
2. Migration 002 now creates the canonical ``upcoming_releases`` schema
   (matching db/schema.py), and migration 006 upgrades existing installs that
   applied the old minimal 002 (audit finding #3).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# 1. Login: no plaintext password fallback
# ---------------------------------------------------------------------------

@pytest.fixture()
def configured_with_users(monkeypatch):
    """App configured with a Navidrome user whose stored password is known.

    ui_routes imports ``get_config`` at module scope, so we patch the route
    module's reference (not helpers.config_helpers.get_config).
    """
    monkeypatch.setattr(
        "helpers.config_helpers.needs_setup",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        "routes.ui_routes.get_config",
        lambda *a, **kw: {
            "navidrome_users": [
                {"base_url": "http://navidrome:4533", "user": "admin", "pass": "stored-pass"},
            ],
        },
    )


@pytest.mark.asyncio
async def test_login_rejects_stored_password_when_navidrome_down(
    configured_with_users, unauthed_client
):
    """The plaintext fallback is gone: submitting the stored config password
    must NOT log in when Navidrome cannot verify it (live ping fails)."""
    class _DownClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            return False

    with patch("api_clients.navidrome.NavidromeClient", _DownClient):
        response = await unauthed_client.post(
            "/login",
            data={"username": "admin", "password": "stored-pass"},
        )
    # Not redirected to the dashboard — login failed.
    assert response.status_code in (200, 401)
    assert "dashboard" not in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_login_succeeds_with_live_navidrome_verification(
    configured_with_users, unauthed_client
):
    """A working live Navidrome check still logs the user in."""
    class _UpClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            return True

    with patch("api_clients.navidrome.NavidromeClient", _UpClient):
        response = await unauthed_client.post(
            "/login",
            data={"username": "admin", "password": "whatever"},
        )
    assert response.status_code in (301, 302)
    assert "/dashboard" in response.headers.get("Location", "")


@pytest.mark.asyncio
async def test_login_wrong_username_rejected(configured_with_users, unauthed_client):
    """A username not in the configured users is rejected."""
    class _UpClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            return True

    with patch("api_clients.navidrome.NavidromeClient", _UpClient):
        response = await unauthed_client.post(
            "/login",
            data={"username": "nobody", "password": "whatever"},
        )
    assert "dashboard" not in response.headers.get("Location", "")


# ---------------------------------------------------------------------------
# 2. Migration 002 / 006 schema sync
# ---------------------------------------------------------------------------

def _migration_columns(module_name: str) -> set[str]:
    """Column names created by a migration's upgrade()."""
    import importlib
    import re

    mod = importlib.import_module(module_name)
    src = open(mod.__file__, encoding="utf-8").read()
    return set(re.findall(r'sa\.Column\("([^"]+)"', src))


def test_migration_002_matches_schema_columns():
    """Migration 002 must create every column the canonical schema declares
    (db/schema.py DDL + COLUMN_REGISTRY) so a fresh migration-only build gets
    the full table — including the columns the writers use (mbid_*,
    artist_in_collection, status, last_seen_at, updated_at, …)."""
    from db.schema import COLUMN_REGISTRY, _read_yaml  # noqa: F401  (module import sanity)

    migration_cols = _migration_columns("migrations.versions.002_add_upcoming_releases")

    # The canonical schema's full column set.
    schema_cols = {
        "id", "artist_name", "album_name", "source", "source_key",
        "release_date", "release_year", "artist_in_collection",
        "album_in_collection", "release_group_mbid", "match_source",
        "primary_type", "mbid_match_status", "mbid_source", "mbid_confidence",
        "mbid_match_score", "mbid_last_checked_at", "mbid_manual_override",
        "candidate_release_group_mbid", "status", "last_seen_at",
        "created_at", "updated_at",
    }
    assert schema_cols.issubset(migration_cols)
    # The two-column unique key must be present (ON CONFLICT (artist, album)).
    assert "uq_upcoming_artist_album" in migration_cols or "uq_upcoming_artist_album" in open(
        __import__("migrations.versions.002_add_upcoming_releases", fromlist=["__file__"]).__file__,
        encoding="utf-8",
    ).read()


def test_migration_006_upgrades_existing_schema():
    """Migration 006 (upgrade) must reference every column the canonical
    schema needs so existing installs that ran the old minimal 002 are
    brought up to the full schema."""
    migration_cols = _migration_columns("migrations.versions.006_sync_upcoming_releases")

    expected = {
        "source_key", "release_year", "artist_in_collection",
        "album_in_collection", "mbid_match_status", "mbid_source",
        "mbid_confidence", "mbid_match_score", "mbid_last_checked_at",
        "mbid_manual_override", "candidate_release_group_mbid", "status",
        "last_seen_at", "updated_at",
    }
    assert expected.issubset(migration_cols)


def test_migration_chain_is_linear():
    """001 → 002 → 003 → 004 → 005 → 006 must be a single linear chain."""
    import re
    from pathlib import Path

    versions = Path("migrations/versions")
    revs: dict[str, str | None] = {}
    for f in versions.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        m_rev = re.search(r'^revision: str = "([^"]+)"', src, re.M)
        m_down = re.search(r'^down_revision: Union\[str, None\] = "([^"]+)"', src, re.M)
        if m_rev:
            revs[m_rev.group(1)] = m_down.group(1) if m_down else None

    # Build the chain from the head (006).
    chain = []
    current = "006_sync_upcoming_releases"
    for _ in range(len(revs)):
        chain.append(current)
        current = revs[current]  # type: ignore[index]
        if current is None:
            break
    assert chain == [
        "006_sync_upcoming_releases",
        "005_add_user_favourites",
        "004_add_folder_matches",
        "003_add_essentia_scan_columns",
        "002_add_upcoming_releases",
        "001_initial_schema",
    ]
