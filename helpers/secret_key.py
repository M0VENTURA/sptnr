"""Stable, shared session secret key resolution.

Quart signs session cookies with ``app.secret_key``.  With hypercorn running
multiple workers, every worker MUST use the same key or a cookie issued by
one worker fails validation on another — and the user is redirected back to
the login screen on the very next page navigation.

Resolution order:
1. ``SECRET_KEY`` env var (explicit operator control — recommended).
2. A persisted key file inside the config directory (``.secret_key``),
   generated once on first boot and shared by every worker.  Because the
   config directory is a shared volume across workers, all processes read
   the same value.
3. As a last resort (read-only / ephemeral config dir), a stable value
   derived from the config path + hostname so sessions still survive worker
   rotation without ever silently falling back to a fresh random key.
"""

from __future__ import annotations

import hashlib
import os
import secrets


def _config_dir() -> str:
    config_path = os.environ.get("CONFIG_PATH") or "/config/config.yaml"
    return os.path.dirname(config_path)


def _key_file_path() -> str:
    return os.path.join(_config_dir(), ".secret_key")


def _read_persisted_key() -> str | None:
    path = _key_file_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def _persist_key(key: str) -> bool:
    """Atomically write the generated key so partial reads are impossible."""
    path = _key_file_path()
    tmp = path + f".{os.getpid()}.tmp"
    try:
        os.makedirs(_config_dir(), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return False


def _derived_fallback() -> str:
    """Stable key when the config dir cannot hold a persisted file.

    Derived from the config path (unique per install) plus the hostname so
    all workers on the same host agree, without a shared file.  This is a
    degraded mode — the value is inferable from the host — so we only reach
    it when the config directory is read-only or ephemeral.
    """
    seed = f"{os.environ.get('CONFIG_PATH', '/config/config.yaml')}::{os.uname().nodename}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def resolve_secret_key() -> str:
    """Return the session secret key, shared across all workers."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key

    persisted = _read_persisted_key()
    if persisted:
        return persisted

    key = secrets.token_hex(32)
    if _persist_key(key):
        return key

    return _derived_fallback()
