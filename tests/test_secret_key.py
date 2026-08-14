"""Tests for the stable shared session secret key.

Regression for: "kept getting pushed back to the login screen when browsing
different sections of the app."  Root cause: every hypercorn worker rolled a
fresh random ``app.secret_key`` when ``SECRET_KEY`` was unset, so a cookie
signed by worker A failed validation on worker B → redirect to login.

Verifies the key is identical across simulated workers and respects the
resolution order (env var > persisted file > stable fallback).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point the helper at a throwaway config dir and clear SECRET_KEY."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))


def test_key_identical_across_workers():
    """Two 'workers' resolving the key must get the SAME value."""
    from helpers.secret_key import resolve_secret_key

    worker_a = resolve_secret_key()
    worker_b = resolve_secret_key()
    assert worker_a == worker_b
    assert len(worker_a) >= 32


def test_env_var_wins():
    """SECRET_KEY env var is authoritative."""
    import os

    from helpers.secret_key import resolve_secret_key

    os.environ["SECRET_KEY"] = "operator-set-key"
    assert resolve_secret_key() == "operator-set-key"


def test_persisted_key_shared_on_disk():
    """The generated key is persisted so separate processes reuse it."""
    from helpers.secret_key import _key_file_path, _read_persisted_key, resolve_secret_key

    key = resolve_secret_key()
    assert _read_persisted_key() == key
    assert open(_key_file_path(), encoding="utf-8").read().strip() == key


def test_read_only_config_dir_still_stable(monkeypatch, tmp_path):
    """If the key file cannot be written, the fallback is stable, not random."""
    from helpers.secret_key import resolve_secret_key

    def _deny(*_a, **_kw):
        return False

    monkeypatch.setattr("helpers.secret_key._persist_key", _deny)
    monkeypatch.setattr("helpers.secret_key._read_persisted_key", lambda: None)

    a = resolve_secret_key()
    b = resolve_secret_key()
    assert a == b


def test_uname_fallback_differs_between_hosts():
    """The no-file fallback must be unique per host, not a shared constant."""
    import os

    from helpers import secret_key

    class _Node:
        nodename = "host-one"

    monkey = pytest.MonkeyPatch()
    monkey.setattr(os, "uname", lambda: _Node())
    key1 = secret_key._derived_fallback()

    class _Node2:
        nodename = "host-two"

    monkey2 = pytest.MonkeyPatch()
    monkey2.setattr(os, "uname", lambda: _Node2())
    key2 = secret_key._derived_fallback()
    assert key1 != key2
