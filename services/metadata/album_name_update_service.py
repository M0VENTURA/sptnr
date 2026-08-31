"""Album-name cleaning + write-back during popularity scans.

Driven by the ``metadata_update`` config block ("Updating Metadata" section
on the Config page):

- ``album_name_source`` — ``album`` (clean the current name) or ``release``
  (prefer the MusicBrainz release title when a confident match exists).
- ``album_name_update_target`` — ``db`` (tracks table only) or ``files``
  (tracks table AND the ALBUM tag on the audio files).
- ``update_on_files`` — per-field file-tag write switches; ``album_name``
  gates the file write for the album name specifically.

The scan calls :func:`clean_album_name_for_scan` once per album; when the
cleaned name differs from the stored name, the tracks rows are updated and,
if configured, the audio file ALBUM tags are rewritten (Navidrome reads
file tags, so the new name then re-serves from Navidrome too).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def get_metadata_update_config() -> dict[str, Any]:
    """Thin wrapper around the config helper (avoids circular imports)."""
    from helpers.config_helpers import get_metadata_update_config as _get
    return _get()


def _cleaned_name(current: str) -> str:
    """Strip repeated trailing edition markers from an album name."""
    from helpers.normalization_service import strip_album_edition_marker
    return strip_album_edition_marker(current or "")


def _release_title_for_album(artist: str, album: str) -> str | None:
    """Return a confident MusicBrainz release title for the album, or None.

    Uses the release-group search; only returns a title when the best match
    clears the confidence floor (``match_score``) and differs from the
    cleaned album name — "Release Name" should only win over the current
    name when MusicBrainz is genuinely confident about the release.
    """
    try:
        from services.enrichment.musicbrainz_service import get_shared_mb_service
        svc = get_shared_mb_service()
        if not svc.enabled:
            return None
        matches = svc.search_releasegroup_matches(artist, album, limit=5) or []
        if not matches:
            return None
        best = matches[0]
        # Confidence floor: a match_score around 0.5+ means the title+artist
        # genuinely matched; below that the search is noise and renaming to
        # it would be wrong.
        try:
            score = float(best.get("match_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < 0.5:
            return None
        title = str(best.get("title") or "").strip()
        if not title:
            return None
        # Never "correct" to something that looks like the same name.
        cleaned_current = _cleaned_name(album)
        from helpers.normalization_service import normalize_title_for_lookup
        if normalize_title_for_lookup(title) == normalize_title_for_lookup(cleaned_current):
            return None
        return title
    except Exception as exc:
        logger.debug(
            "MB release-title lookup failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return None


def resolve_album_name(
    *,
    artist: str,
    album: str,
    config: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Resolve the target album name + the reason it changed.

    Returns ``(new_name, reason)`` where ``reason`` is None when the name is
    unchanged (no write needed).
    """
    cfg = config or get_metadata_update_config()
    source = str(cfg.get("album_name_source") or "album").strip().lower()

    cleaned = _cleaned_name(album)
    if source == "release":
        release_title = _release_title_for_album(artist, album)
        if release_title:
            return release_title, "release"

    if cleaned != (album or "").strip():
        return cleaned, "cleaned"

    return album, None


def update_album_name_in_db(
    *,
    artist: str,
    album: str,
    new_name: str,
) -> int:
    """Rewrite the album name for every track of the album in the DB.

    Returns the number of rows updated (0 when nothing changed).
    """
    if not artist or not album or not new_name or new_name == album:
        return 0
    try:
        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks
                    SET album = :new_name
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                """),
                {"artist": artist, "album": album, "new_name": new_name},
            )
            return result.rowcount or 0
    except Exception as exc:
        logger.debug(
            "[ALBUM_NAME] DB update failed",
            artist=artist,
            album=album,
            new_name=new_name,
            error=str(exc),
        )
        return 0


def update_album_name_in_files(
    *,
    artist: str,
    album: str,
    new_name: str,
) -> int:
    """Rewrite the ALBUM tag on the audio files of the album.

    Returns the number of files written.
    """
    if not artist or not album or not new_name or new_name == album:
        return 0
    written = 0
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                      AND file_path IS NOT NULL AND TRIM(file_path) <> ''
                """),
                {"artist": artist, "album": album},
            ).fetchall() or []

        for row in rows:
            fp = str(row[0] or "")
            if not fp:
                continue
            try:
                # Only the ALBUM frame is rewritten — passing a dict with
                # other fields None would make the FLAC/MP3 writer DELETE
                # those frames (None = "clear this field").
                from services.metadata.tag_file_service import write_tags_to_file
                ok = write_tags_to_file(
                    fp,
                    {"album": new_name},
                )
                if ok:
                    written += 1
            except Exception as exc:
                logger.debug(
                    "[ALBUM_NAME] File tag write failed",
                    file_path=fp,
                    error=str(exc),
                )
    except Exception as exc:
        logger.debug(
            "[ALBUM_NAME] File-list load failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
    return written


def apply_album_name_update(
    *,
    artist: str,
    album: str,
    new_name: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a resolved album-name rename to the DB and (optionally) files.

    Returns a summary dict:
    ``{"changed": bool, "new_name": str, "reason": str|None, "db_updated": int, "files_updated": int}``

    Safe no-op when ``new_name`` equals the current name.
    """
    cfg = config or get_metadata_update_config()
    result: dict[str, Any] = {
        "changed": False,
        "new_name": new_name,
        "reason": None,
        "db_updated": 0,
        "files_updated": 0,
    }
    if not album or not artist or not new_name or new_name == album:
        return result

    db_updated = update_album_name_in_db(artist=artist, album=album, new_name=new_name)

    files_updated = 0
    target = str(cfg.get("album_name_update_target") or "db").strip().lower()
    update_on_files = (cfg.get("update_on_files") or {}).get("album_name", False)
    if target == "files" and update_on_files:
        files_updated = update_album_name_in_files(
            artist=artist,
            album=album,
            new_name=new_name,
        )

    if db_updated or files_updated:
        result.update({
            "changed": True,
            "new_name": new_name,
            "db_updated": db_updated,
            "files_updated": files_updated,
        })

    return result


def clean_album_name_for_scan(
    *,
    artist: str,
    album: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve + apply the configured album-name cleaning in one call.

    Convenience wrapper around :func:`resolve_album_name` +
    :func:`apply_album_name_update` for one-shot / manual use.  The scan
    runner uses the two-step form so the DB rename can be DEFERRED until
    after the artist's star-rating finalise.

    Returns the :func:`apply_album_name_update` summary (``changed`` False
    when nothing needs renaming).
    """
    cfg = config or get_metadata_update_config()
    if not album or not artist:
        return {
            "changed": False,
            "new_name": album,
            "reason": None,
            "db_updated": 0,
            "files_updated": 0,
        }

    new_name, reason = resolve_album_name(artist=artist, album=album, config=cfg)
    if not reason or new_name == album:
        return {
            "changed": False,
            "new_name": album,
            "reason": None,
            "db_updated": 0,
            "files_updated": 0,
        }

    result = apply_album_name_update(
        artist=artist,
        album=album,
        new_name=new_name,
        config=cfg,
    )
    result["reason"] = reason
    if result.get("changed"):
        log_unified_msg = (
            f"[ALBUM_NAME] '{artist} - {album}' → '{new_name}' "
            f"(reason={reason}, db={result.get('db_updated')}, "
            f"files={result.get('files_updated')})"
        )
        try:
            from helpers.logging_config import log_unified
            log_unified(log_unified_msg)
        except Exception:
            logger.info("[ALBUM_NAME] %s", log_unified_msg)

    return result
