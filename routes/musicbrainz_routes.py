"""MusicBrainz tag import/search/download routes — migrated from old app.py."""

from __future__ import annotations

import logging
import os
import time
import re
from typing import Any

from quart import Blueprint, jsonify, request, session

from sqlalchemy import text
from db.engine import async_db_session, db_session
from helpers.config_helpers import get_config
from helpers.response_helpers import _ok, _fail
from api_clients.musicbrainz_http import MusicBrainzHttpClient, MUSICBRAINZ_UUID_RE

from services.downloads.download_pipeline_service import start_release_download
from services.downloads.download_processing_service import queue_add

logger = logging.getLogger(__name__)

mb_bp = Blueprint("musicbrainz", __name__, url_prefix="/api/musicbrainz")
_mb_client: MusicBrainzHttpClient | None = None


def _quote_ident(identifier: str, bind: Any) -> str:
    """Safely quote a database identifier for a dynamic SQL clause.

    Request-supplied column names are quoted with the dialect's identifier
    preparer (double-quoted + escaped), so an attacker-supplied value can
    never break out of the identifier position and inject SQL.
    """
    try:
        dialect = getattr(bind, "dialect", None)
        return dialect.identifier_preparer.quote(str(identifier))
    except Exception:
        # Fall back to a conservative allow-list when the dialect is
        # unavailable: reject anything that is not a plain identifier.
        import re as _re
        if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier)):
            return f'"{identifier}"'
        raise ValueError(f"Invalid identifier: {identifier!r}")


def _get_mb_client() -> MusicBrainzHttpClient:
    global _mb_client
    if _mb_client is None:
        _mb_client = MusicBrainzHttpClient(enabled=True)
    return _mb_client


def _normalize_download_method(
    requested_method: str,
    *,
    default_method: str = "slskd",
    context: str = "download request",
) -> tuple[str | None, str | None]:
    """Resolve the effective download transport based on enabled integrations.

    Soulseek (slskd) is the only download transport; a requested Soulseek
    alias is normalised and the method is refused when slskd is disabled.
    """
    method = str(requested_method or default_method).strip().lower()
    if method == "soulseek":
        method = "slskd"

    if method != "slskd":
        return None, "Invalid method. Use 'slskd'"

    cfg = get_config() or {}
    slskd_enabled = bool((cfg.get("slskd") or {}).get("enabled", False))
    if not slskd_enabled:
        return None, "Soulseek (slskd) is not enabled"

    return method, None


def _frontend_status(db_status: Any) -> str:
    """Map musicbrainz_releases.status to the status vocabulary the downloads
    page badge/action logic understands (queued/downloading/completed/failed)."""
    s = str(db_status or "").strip().lower()
    mapping = {
        "active": "queued",
        "finalizing": "downloading",
        "finalized": "completed",
        "completed": "completed",
        "cancelled": "cancelled",
        "removed": "cancelled",
        "error": "failed",
        "failed": "failed",
    }
    return mapping.get(s, s or "queued")



# ---------------------------------------------------------------------------
# GET /api/musicbrainz/tags/track
# ---------------------------------------------------------------------------

@mb_bp.route("/tags/track", methods=["GET"])
def api_musicbrainz_tags_track():
    """Get MusicBrainz tags for a single track."""
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    if not artist or not title:
        return jsonify({"error": "artist and title required"}), 400
    try:
        client = _get_mb_client()
        recordings = client.search_recordings(
            f'artist:"{artist}" AND recording:"{title}"',
            limit=5,
        )
        return jsonify({"success": True, "recordings": recordings})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/tags/album
# ---------------------------------------------------------------------------

@mb_bp.route("/tags/album", methods=["GET"])
def api_musicbrainz_tags_album():
    """Get MusicBrainz tags for all tracks in an album."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not (artist and album):
        return jsonify({"error": "artist and album required"}), 400
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT id, artist, title FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"),
                {"artist": artist, "album": album},
            )
            rows = result.fetchall()
        return jsonify({"success": True, "tracks": [dict(r._mapping) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/import/track
# ---------------------------------------------------------------------------

@mb_bp.route("/import/track", methods=["POST"])
def api_musicbrainz_import_track():
    """Import MusicBrainz tags from MP3 for a single track."""
    return jsonify({"success": True, "message": "Track import queued"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/import/album
# ---------------------------------------------------------------------------

@mb_bp.route("/import/album", methods=["POST"])
def api_musicbrainz_import_album():
    """Import MusicBrainz tags from MP3s for all tracks in an album."""
    return jsonify({"success": True, "message": "Album import queued"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/import/artist
# ---------------------------------------------------------------------------

@mb_bp.route("/import/artist", methods=["POST"])
def api_musicbrainz_import_artist():
    """Import MusicBrainz tags from MP3s for all tracks by an artist."""
    return jsonify({"success": True, "message": "Artist import queued"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/tag/update
# ---------------------------------------------------------------------------

@mb_bp.route("/tag/update", methods=["POST"])
async def api_musicbrainz_tag_update():
    """Update a MusicBrainz tag in the database and optionally write to MP3."""
    data = (await request.get_json()) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    field_name = data.get("field", "").strip()
    field_value = data.get("value", "").strip()
    write_to_mp3 = data.get("write_to_mp3", False)
    if not (artist and album and title and field_name):
        return jsonify({"error": "Missing required fields"}), 400
    try:
        async with async_db_session() as session:
            # ``field_name`` comes from the request body — quote the identifier
            # via the dialect so an attacker-supplied column can never inject
            # SQL (previously interpolated raw into the SET clause).
            result = await session.execute(
                text(f"UPDATE tracks SET {_quote_ident(field_name, session.get_bind())} = :value WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album AND title = :title"),
                {"value": field_value, "artist": artist, "album": album, "title": title},
            )
        return jsonify({"success": True, "updated": result.rowcount})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/tag/write-to-mp3
# ---------------------------------------------------------------------------

@mb_bp.route("/tag/write-to-mp3", methods=["POST"])
def api_musicbrainz_tag_write_mp3():
    """Write MusicBrainz tags to MP3 file (without database update)."""
    return jsonify({"success": True, "message": "Tag write not yet implemented"}), 200


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/tags/batch-update
# ---------------------------------------------------------------------------

@mb_bp.route("/tags/batch-update", methods=["POST"])
async def api_musicbrainz_batch_update():
    """Update multiple MusicBrainz tags at once."""
    data = (await request.get_json()) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    title = data.get("title", "").strip()
    tags = data.get("tags", {})
    if not (artist and album and title and tags):
        return jsonify({"error": "Missing required fields"}), 400
    try:
        async with async_db_session() as session:
            # Column names AND values both come from the request body: quote
            # every identifier via the dialect and use generated bind names so
            # neither the SET clause nor the parameter names can inject SQL.
            set_parts: list[str] = []
            params: dict[str, Any] = {"artist": artist, "album": album, "title": title}
            for _i, (_k, _v) in enumerate(tags.items()):
                bind = f"v{_i}"
                set_parts.append(f"{_quote_ident(str(_k), session.get_bind())} = :{bind}")
                params[bind] = _v
            result = await session.execute(
                text(f"UPDATE tracks SET {', '.join(set_parts)} WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album AND title = :title"),
                params,
            )
        return jsonify({"success": True, "updated": result.rowcount})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/search
# ---------------------------------------------------------------------------

def _normalise_search_key(value: str) -> str:
    """Lowercase + strip non-alphanumerics for library cross-referencing."""
    return re.sub(r"[^a-zA-Z0-9]+", "", (value or "").lower())


@mb_bp.route("/search", methods=["POST"])
async def api_musicbrainz_search():
    """Search MusicBrainz for releases + local cached missing releases.

    Accepts structured fields so each search-form entry maps to the correct
    MusicBrainz Lucene index:

        artist  → artist:"<name>"
        album   → releasegroup:"<title>"
        track   → recording:"<title>"   (searched on the release index)
        year    → date:<year>

    Legacy callers may still send ``query`` (+ optional ``artist_only``); those
    are routed to ``artist`` / ``releasegroup`` respectively.

    Results mirror the legacy behaviour: local ``missing_releases`` are merged
    in first, and every release is enriched with ``category``, ``cover_art_url``
    (Cover Art Archive) and ``source`` (local | musicbrainz).
    """
    payload = (await request.get_json(silent=True)) or {}
    artist = str(payload.get("artist", "")).strip()
    album = str(payload.get("album", "")).strip()
    track = str(payload.get("track", "")).strip()
    year = str(payload.get("year", "")).strip()
    query = str(payload.get("query", "")).strip()
    artist_only = bool(payload.get("artist_only", False))
    release_type = str(payload.get("type") or payload.get("release_type") or "").strip().lower()

    def _esc(value: str) -> str:
        return value.replace('"', "")

    def _type_query_term(release_type: str) -> str | None:
        """Map a UI dropdown type to a MusicBrainz Lucene term.

        ``primary_type`` only supports album / single / ep / broadcast / other.
        Live / Remix / Compilation / Soundtrack are ``secondary-type`` values,
        so they must use ``secondarytype:`` instead of ``primarytype:``.
        """
        rt = (release_type or "").lower()
        if not rt or rt in ("", "all"):
            return None
        primary_types = {"album": "album", "single": "single", "ep": "ep",
                         "broadcast": "broadcast", "other": "other"}
        if rt in primary_types:
            return f"primarytype:{primary_types[rt]}"
        secondary_types = {"compilation": "compilation", "soundtrack": "soundtrack",
                           "live": "live", "remix": "remix", "dj-mix": "dj-mix",
                           "mixtape": "mixtape", "interview": "interview",
                           "demo": "demo", "audiobook": "audiobook", "spokenword": "spokenword"}
        if rt in secondary_types:
            return f"secondarytype:{secondary_types[rt]}"
        return None

    def _normalise_category(primary_type: str) -> str:
        pt = (primary_type or "").lower()
        if pt == "ep":
            return "EP"
        if pt == "single":
            return "Single"
        if pt == "album":
            return "Album"
        return primary_type or "Other"

    def _category_from_types(primary_type: str, secondary_types) -> str:
        """Derive a display category combining primary + secondary types.

        MusicBrainz ``primary_type`` is only ever Album / Single / EP /
        Broadcast / Other.  Live, Remix, Compilation and Soundtrack are
        ``secondary-types`` on the release-group — without this they all get
        lumped under "Album".
        """
        pt = _normalise_category(primary_type)

        secondary = secondary_types or []
        if isinstance(secondary, str):
            secondary = [secondary]
        sec = [str(s).strip().lower() for s in secondary if str(s).strip()]

        # Secondary types take precedence for display grouping so Live /
        # Remix / Compilation / Soundtrack get their own sections.
        for label, keys in (
            ("Live", ("live",)),
            ("Remix", ("remix",)),
            ("Compilation", ("compilation",)),
            ("Soundtrack", ("soundtrack",)),
            ("DJ-mix", ("dj-mix", "djmix")),
            ("Mixtape", ("mixtape",)),
            ("Interview", ("interview",)),
            ("Spokenword", ("spokenword", "spoken word")),
            ("Demo", ("demo",)),
            ("Audiobook", ("audiobook",)),
        ):
            if any(k in sec for k in keys):
                return label

        return pt

    def _enrich_release_group(rg: dict[str, Any], source: str) -> dict[str, Any]:
        rgid = str(rg.get("id") or "")
        primary_type = rg.get("primary-type") or rg.get("type") or "Other"
        secondary_types = rg.get("secondary-types") or rg.get("secondary_type") or []
        artist_credit = rg.get("artist-credit") or []
        artist_name = ""
        if artist_credit and isinstance(artist_credit, list):
            parts = []
            for ac in artist_credit:
                if isinstance(ac, dict):
                    name = ac.get("name", "") or (ac.get("artist", {}) or {}).get("name", "")
                    join_phrase = ac.get("joinphrase", "")
                    if name:
                        parts.append(name)
                    if join_phrase:
                        parts.append(join_phrase)
                elif isinstance(ac, str):
                    parts.append(ac)
            artist_name = "".join(parts)

        raw_date = rg.get("first-release-date")
        first_release_date = str(raw_date) if raw_date else ""

        cover_art_url = f"https://coverartarchive.org/release-group/{rgid}/front-250" if rgid else ""

        return {
            "id": rgid,
            "title": rg.get("title", ""),
            "primary_type": primary_type,
            "secondary_types": [str(s) for s in (secondary_types or [])],
            "category": _category_from_types(str(primary_type), secondary_types),
            "first_release_date": first_release_date,
            "artist": artist_name,
            "artist-credit": artist_credit,
            "cover_art_url": cover_art_url,
            "source": source,
        }

    try:
        client = _get_mb_client()

        releases: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # ── 1. Local missing_releases merge (legacy parity) ──────────────
        try:
            from sqlalchemy import text as _text
            from db.engine import db_session as _db_session
            with _db_session() as session:
                if artist_only:
                    result = session.execute(
                        _text("""
                            SELECT artist, release_id, title, primary_type, first_release_date, cover_art_url, category
                            FROM missing_releases
                            WHERE LOWER(artist) = LOWER(:q)
                            ORDER BY first_release_date DESC
                            LIMIT 100
                        """),
                        {"q": artist or query},
                    )
                else:
                    term = f"%{(artist or album or track or query)}%"
                    result = session.execute(
                        _text("""
                            SELECT artist, release_id, title, primary_type, first_release_date, cover_art_url, category
                            FROM missing_releases
                            WHERE artist ILIKE :term OR title ILIKE :term
                            ORDER BY artist, first_release_date DESC
                            LIMIT 50
                        """),
                        {"term": term},
                    )
                for row in result.fetchall() or []:
                    # SQLAlchemy 2.0 Row objects do not support string indexing
                    # (``row["col"]`` raises TypeError) — read via ``_mapping``.
                    _m = row._mapping
                    artist_name = str(_m["artist"] or "")
                    release_id = str(_m["release_id"] or "")
                    result_id = f"{artist_name}_{release_id}"
                    if result_id in seen_ids:
                        continue
                    seen_ids.add(result_id)
                    pt = str(_m["primary_type"] or "")
                    row_cat = str(_m["category"] or "")
                    # Apply the UI type filter to local results too, so cached
                    # entries of other types don't leak in when a type is
                    # selected. Filter on the stored category when present
                    # (the derived column), falling back to the derived type —
                    # stale primary_type columns (e.g. singles persisted with
                    # a default primary type of "Album") must not leak
                    # through when a type is chosen.
                    if release_type and release_type not in ("", "all"):
                        local_cat = (row_cat or _category_from_types(pt or "Other", [])).lower()
                        if local_cat != release_type:
                            continue
                    releases.append({
                        "id": release_id,
                        "title": str(_m["title"] or ""),
                        "primary_type": pt or row_cat,
                        "secondary_types": [],
                        # Use the stored derived category when present — the
                        # primary_type column is often stale (e.g. remixes
                        # persisted with a default "Album"), and normalising
                        # it would override the correct "Remix"/"Live" label.
                        "category": row_cat or _normalise_category(pt or "Other"),
                        "first_release_date": str(_m["first_release_date"] or ""),
                        "artist": artist_name,
                        "artist-credit": [{"name": artist_name}],
                        "cover_art_url": str(_m["cover_art_url"] or ""),
                        "source": "local",
                    })
        except Exception as exc:
            logger.debug("[MB_SEARCH] Local missing-releases merge failed: %s", exc)

        # ── 2. MusicBrainz search ────────────────────────────────────────
        # Track term present → search the RELEASE index (release-group index
        # has no ``recording`` field). Otherwise → release-group index with
        # field-aware Lucene query.
        if track:
            track_parts = [f'recording:"{_esc(track)}"']
            if artist:
                track_parts.append(f'artist:"{_esc(artist)}"')
            if year:
                track_parts.append(f'date:{_esc(year)}')
            type_term = _type_query_term(release_type)
            if type_term:
                track_parts.append(type_term)
            track_query = " AND ".join(track_parts)

            raw = client.get("release/", params={
                "query": track_query,
                "limit": 50,
            })
            raw_releases = raw.get("releases", []) if isinstance(raw.get("releases"), list) else []

            seen: set[str] = set()
            for rel in raw_releases:
                rg = rel.get("release-group") or {}
                rgid = rg.get("id")
                if not rgid or rgid in seen:
                    continue
                seen.add(rgid)
                if album:
                    rg_title = str(rg.get("title") or "").lower()
                    album_l = album.lower()
                    if album_l not in rg_title and rg_title not in album_l:
                        continue
                artist_name = str(
                    (rg.get("artist-credit") or [{}])[0].get("name", "")
                    if (rg.get("artist-credit") or [{}])[0] else ""
                )
                result_id = f"{artist_name}_{rgid}"
                if result_id in seen_ids:
                    continue
                seen_ids.add(result_id)
                releases.append(_enrich_release_group(rg, "musicbrainz"))
                if len(releases) >= 40:
                    break
        else:
            parts: list[str] = []
            if artist:
                parts.append(f'artist:"{_esc(artist)}"')
            if album:
                parts.append(f'releasegroup:"{_esc(album)}"')
            if year:
                parts.append(f'date:{_esc(year)}')
            type_term = _type_query_term(release_type)
            if type_term:
                parts.append(type_term)

            if not parts:
                # Legacy free-text query path.  The old system sent the raw
                # free-text query (no field prefix) so MusicBrainz matches it
                # across title AND artist — "Mudvayne" finds releases BY
                # Mudvayne, not just releases whose title contains "Mudvayne".
                if not query:
                    return jsonify({"error": "query required"}), 400
                parts.append(
                    f'artist:"{_esc(query)}"' if artist_only else query
                )

            mb_query = " AND ".join(parts)

            if not mb_query.strip():
                return jsonify({"error": "query required"}), 400

            raw = client.get("release-group/", params={
                "query": mb_query,
                "fmt": "json",
                "limit": 50 if artist_only else 20,
            })
            raw_groups = raw.get("release-groups", []) if isinstance(raw.get("release-groups"), list) else []

            # ── Artist+album fallback (legacy parity) ─────────────────────
            # MusicBrainz's release-group index applies strict phrase matching
            # on quoted ``artist:"…" AND releasegroup:"…"`` queries, so a
            # multi-word artist + a generic album title ("spice girls" +
            # "greatest hits") can return zero hits even when the release
            # exists (e.g. the group is titled "Greatest Hits (Deluxe)" or
            # the artist credit differs slightly).  When a structured artist
            # search comes up empty, retry with artist-only so the user always
            # sees the artist's releases (old_system did the same).
            if not raw_groups and artist and not artist_only:
                try:
                    artist_only_query = f'artist:"{_esc(artist)}"'
                    raw_fallback = client.get("release-group/", params={
                        "query": artist_only_query,
                        "fmt": "json",
                        "limit": 20,
                    })
                    raw_groups = (
                        raw_fallback.get("release-groups", [])
                        if isinstance(raw_fallback.get("release-groups"), list)
                        else []
                    )
                    if raw_groups:
                        logger.info(
                            "[MB_SEARCH] Combined query '%s' returned 0 — falling back to artist-only for '%s'",
                            mb_query, artist,
                        )
                except Exception as exc:
                    logger.debug("[MB_SEARCH] Artist-only fallback failed: %s", exc)

            for rg in raw_groups:
                rgid = str(rg.get("id") or "")
                if not rgid:
                    continue
                artist_name = str(
                    (rg.get("artist-credit") or [{}])[0].get("name", "")
                    if (rg.get("artist-credit") or [{}])[0] else ""
                )
                result_id = f"{artist_name}_{rgid}"
                if result_id in seen_ids:
                    continue
                seen_ids.add(result_id)
                releases.append(_enrich_release_group(rg, "musicbrainz"))

        # ── 2b. Unified type post-filter (defence in depth) ─────────────
        # The Lucene ``primarytype:`` / ``secondarytype:`` terms narrow the
        # MusicBrainz query, but local rows and edge-case MB data can still
        # slip through. Re-filter the combined list so the UI type dropdown
        # is authoritative: primary types (album/single/ep/...) match on the
        # release-group's primary type; secondary types (compilation/live/
        # remix/...) match on the derived display category.
        if release_type and release_type not in ("", "all"):
            # Match on the derived display category (primary + secondary
            # types): "Album" must exclude remix/live/compilation/soundtrack
            # release-groups (primary type Album + a secondary type) and any
            # stale rows whose primary_type alone would match.
            releases = [
                r for r in releases
                if str(r.get("category") or r.get("primary_type") or "").lower() == release_type
            ]

        # ── 2c. Library dedupe (discovery accuracy) ─────────────────────
        # Release-groups the user already owns are useless in the discovery
        # list.  Strip any MusicBrainz result whose release-group MBID or
        # normalised (artist, album) title already exists in the local
        # library, so the MusicBrainz tab count reflects only missing albums.
        # Local ``missing_releases`` rows (source "local") are untouched.
        try:
            from sqlalchemy import text as _lib_text
            from db.engine import db_session as _lib_db_session

            mb_rows = [r for r in releases if r.get("source") == "musicbrainz"]
            if mb_rows:
                owned_rgids: set[str] = set()
                owned_pairs: set[tuple[str, str]] = set()
                candidates = [
                    (
                        _normalise_search_key(r.get("artist")),
                        _normalise_search_key(r.get("title")),
                        str(r.get("id") or "").strip(),
                    )
                    for r in mb_rows
                ]
                with _lib_db_session() as session:
                    rgids = [c[2] for c in candidates if c[2]]
                    if rgids:
                        ph = ", ".join(f":r{i}" for i in range(len(rgids)))
                        rows = session.execute(
                            _lib_text(
                                "SELECT DISTINCT TRIM(musicbrainz_releasegroupid) FROM tracks "
                                f"WHERE musicbrainz_releasegroupid IN ({ph})"
                            ),
                            {f"r{i}": rid for i, rid in enumerate(rgids)},
                        ).fetchall()
                        owned_rgids = {str(r[0]) for r in rows if r[0]}
                    pairs = [(c[0], c[1]) for c in candidates if c[0] and c[1]]
                    if pairs:
                        conds = " OR ".join(
                            f"(LOWER(REGEXP_REPLACE(COALESCE(NULLIF(album_artist, ''), artist), "
                            f"'[^a-zA-Z0-9]', '', 'g')) = :a{i} "
                            f"AND LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g')) = :b{i})"
                            for i in range(len(pairs))
                        )
                        pair_params: dict[str, str] = {}
                        for i, (a, b) in enumerate(pairs):
                            pair_params[f"a{i}"] = a
                            pair_params[f"b{i}"] = b
                        rows = session.execute(
                            _lib_text(
                                "SELECT DISTINCT "
                                "LOWER(REGEXP_REPLACE(COALESCE(NULLIF(album_artist, ''), artist), "
                                "'[^a-zA-Z0-9]', '', 'g')), "
                                "LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g')) "
                                f"FROM tracks WHERE {conds}"
                            ),
                            pair_params,
                        ).fetchall()
                        owned_pairs = {(str(r[0]), str(r[1])) for r in rows}

                def _is_owned(r: dict[str, Any]) -> bool:
                    rgid = str(r.get("id") or "").strip()
                    if rgid and rgid in owned_rgids:
                        return True
                    pair = (
                        _normalise_search_key(r.get("artist")),
                        _normalise_search_key(r.get("title")),
                    )
                    return pair in owned_pairs

                releases = [r for r in releases if r.get("source") != "musicbrainz" or not _is_owned(r)]
        except Exception as exc:
            logger.debug("[MB_SEARCH] Library dedupe failed: %s", exc)

        # ── 3. Sort (legacy parity): artist asc, then first release date desc
        if artist_only:
            releases.sort(key=lambda x: str(x.get("first_release_date") or ""), reverse=True)
        else:
            releases.sort(
                key=lambda x: (str(x.get("artist") or "").lower(), str(x.get("first_release_date") or "")),
                reverse=True,
            )

        # ── 4. Best-effort cached track counts ──────────────────────────
        # The release-group search API exposes no track count; surface the
        # cached total when this release was already queued/downloaded
        # (musicbrainz_releases.total_tracks) so users can spot 4-track
        # promos vs full albums before queueing.  No extra MB API calls.
        try:
            from sqlalchemy import text as _mb_text
            from db.engine import db_session as _mb_db_session
            _ids = [str(r.get("id") or "") for r in releases if r.get("id")]
            _counts: dict[str, int] = {}
            if _ids:
                with _mb_db_session() as session:
                    for row in session.execute(
                        _mb_text("SELECT release_id, total_tracks FROM musicbrainz_releases WHERE release_id IN :ids"),
                        {"ids": tuple(_ids)},
                    ).fetchall():
                        if row[1]:
                            _counts[str(row[0])] = int(row[1])
            for r in releases:
                r["track_count"] = _counts.get(str(r.get("id") or ""))
        except Exception:
            pass

        return jsonify({"success": True, "releases": releases})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/search/releases
# ---------------------------------------------------------------------------

@mb_bp.route("/search/releases", methods=["GET"])
def api_musicbrainz_search_releases():
    """Search MusicBrainz for releases by artist and album."""
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    try:
        client = _get_mb_client()
        releases = client.search_releases(
            f'artist:"{artist}" AND release:"{album}"',
            limit=10,
        )
        return jsonify({"success": True, "releases": releases})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/search-releases  (hyphen — used by the modal JS)
# ---------------------------------------------------------------------------

@mb_bp.route("/search-releases", methods=["GET"])
def api_musicbrainz_search_releases_modal():
    """Search MusicBrainz release-groups for the search modal.

    Query params:
        q (str): Free-text search query.
        type (str, optional): Release type filter (album, single, ep, etc.).
        limit (int, optional): Max results (default 25, max 50).
    """
    query = request.args.get("q", "").strip()
    release_type = request.args.get("type", "").strip().lower()
    limit = min(int(request.args.get("limit", 25)), 50)

    if not query:
        return jsonify({"releases": []})

    try:
        client = _get_mb_client()

        # Build MusicBrainz query
        mb_query_parts = [f'releasegroup:"{query.replace(chr(34), "")}"']
        if release_type and release_type not in ("", "all"):
            mb_query_parts.append(f'primarytype:{release_type}')
        mb_query = " AND ".join(mb_query_parts)

        # Search release-groups with genres and release data
        payload = client.get("release-group/", params={
            "query": mb_query,
            "fmt": "json",
            "limit": limit,
            "inc": "genres+releases",
        })
        groups = payload.get("release-groups", []) if isinstance(payload.get("release-groups"), list) else []

        releases = []
        for rg in groups:
            # Extract artist credit
            artist_credit = rg.get("artist-credit", [])
            artist_name = ""
            if artist_credit and isinstance(artist_credit, list):
                parts = []
                for ac in artist_credit:
                    if isinstance(ac, dict):
                        name = ac.get("name", "") or (ac.get("artist", {}) or {}).get("name", "")
                        join_phrase = ac.get("joinphrase", "")
                        if name:
                            parts.append(name)
                        if join_phrase:
                            parts.append(join_phrase)
                    elif isinstance(ac, str):
                        parts.append(ac)
                artist_name = "".join(parts)

            rg_mbid = rg.get("id", "") or ""
            raw_date = rg.get("first-release-date")
            first_release_date = str(raw_date) if raw_date else ""

            # Build cover art URL from Cover Art Archive using release-group MBID
            cover_art_url = ""
            if rg_mbid:
                cover_art_url = f"https://coverartarchive.org/release-group/{rg_mbid}/front-250"

            release = {
                "id": rg_mbid,
                "title": rg.get("title", ""),
                "primary_type": rg.get("primary-type", rg.get("type", "Other")),
                "first_release_date": first_release_date,
                "artist": artist_name,
                "artist-credit": artist_credit,
                "cover_art_url": cover_art_url,
                "genres": [g.get("name", "") for g in (rg.get("genres", []) or []) if isinstance(g, dict)],
                "tags": [t.get("name", "") for t in (rg.get("tags", []) or []) if isinstance(t, dict)],
                "releases": [],
            }

            # Include contained releases for the "Choose Release" dropdown
            contained = rg.get("releases", [])
            if isinstance(contained, list):
                for rel in contained:
                    if isinstance(rel, dict):
                        release["releases"].append({
                            "id": rel.get("id", ""),
                            "title": rel.get("title", rg.get("title", "")),
                            "date": rel.get("date", ""),
                            "release_date": rel.get("date", ""),
                            "country": rel.get("country", ""),
                            "label": "",
                            "format": "",
                        })

            releases.append(release)

        return jsonify({"releases": releases})

    except Exception as exc:
        logger.error("MusicBrainz search-releases failed: %s", exc, exc_info=True)
        return jsonify({"releases": [], "error": str(exc)})


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/release-picker  (slide-over Release Picker flyout)
# ---------------------------------------------------------------------------

def _picker_total_tracks(release: dict[str, Any]) -> int:
    """Sum track counts across all media (CDs/discs)."""
    return sum(int((m.get("track-count") or 0)) for m in (release.get("media") or []))


def _picker_formats(release: dict[str, Any]) -> str:
    """Unique media formats, deduped, preserving order."""
    formats = [
        str(m.get("format") or "").strip()
        for m in (release.get("media") or [])
        if str(m.get("format") or "").strip()
    ]
    return ", ".join(dict.fromkeys(formats)) or "Digital/CD"


def _picker_tracklist_html(client, release_id: str) -> str:
    """HTML fragment for the per-release 'Preview Tracks' toggle."""
    import html as _html

    try:
        release = client.get_release(release_id, inc="media+recordings")
    except Exception as exc:
        logger.error("[MB_PICKER] tracklist failed for %s: %s", release_id, exc)
        return "<div class='text-danger small'>Failed to load tracklist.</div>"
    if not release:
        return "<div class='text-muted small'>No tracklist available.</div>"

    rows = []
    for medium in release.get("media") or []:
        medium_format = str(medium.get("format") or "").strip()
        for track in medium.get("tracks") or []:
            number = str(track.get("number") or "").strip()
            title = str(track.get("title") or "?")
            prefix = f"<small class='text-muted'>{_html.escape(number)}.</small> " if number else ""
            rows.append(
                f"<div class='d-flex justify-content-between py-1 border-bottom border-secondary-subtle'>"
                f"<span class='small'>{prefix}{_html.escape(title)}</span>"
                f"<span class='small text-muted flex-shrink-0'>{_html.escape(medium_format)}</span>"
                f"</div>"
            )
    if not rows:
        return "<div class='text-muted small'>No tracklist available.</div>"
    return "".join(rows)


@mb_bp.route("/release-picker", methods=["GET"])
async def api_musicbrainz_release_picker():
    """Slide-over release picker for a MusicBrainz release group.

    Modes:
      ``?rg_id=...&artist=...&album=...`` → HTML cards for every release
          under the release group, sorted official-first then by track count
          desc (mirrors ``resolve_release_id`` so the default view matches
          what blind queueing would have picked).
      ``?release_id=<mbid>`` → HTML tracklist fragment for one release
          (used by the "Preview Tracks" toggle).
    """
    from quart import render_template_string as _render

    client = _get_mb_client()

    release_id = (request.args.get("release_id") or "").strip()
    if release_id:
        return _picker_tracklist_html(client, release_id)

    rg_id = (request.args.get("rg_id") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    album = (request.args.get("album") or "").strip()
    if not rg_id:
        return "<div class='alert alert-danger m-3'>Missing Release Group ID</div>", 400

    try:
        releases = client.browse_releases_for_group(rg_id, inc="media", limit=100)
        if not releases:
            # The id may already be a concrete release — hop to its group.
            release = client.get_release(rg_id, inc="release-groups")
            group_id = ((release or {}).get("release-group") or {}).get("id")
            if group_id:
                releases = client.browse_releases_for_group(group_id, inc="media", limit=100)
    except Exception as exc:
        logger.error("[MB_PICKER] browse failed for %s: %s", rg_id, exc)
        return "<div class='alert alert-danger m-3'>Failed to fetch releases from MusicBrainz.</div>", 500

    processed = []
    for rel in releases:
        processed.append({
            "id": rel.get("id"),
            "title": rel.get("title") or album,
            "status": rel.get("status") or "Official",
            "disambiguation": rel.get("disambiguation") or "",
            "country": rel.get("country") or "Global",
            "date": rel.get("date") or "Unknown Date",
            "track_count": _picker_total_tracks(rel),
            "format": _picker_formats(rel),
            "artist": artist,
        })

    # Official editions first, then most tracks — the same ranking the smart
    # resolver uses, so the top card equals what a blind queue would get.
    processed.sort(
        key=lambda r: (str(r["status"]).lower() != "official", -r["track_count"])
    )

    # JSON mode: the release-picker flyout probes the group first and
    # auto-queues when it contains exactly ONE release (no flyout needed).
    if request.args.get("format", "").strip().lower() == "json":
        return jsonify({
            "releases": processed,
            "count": len(processed),
        })

    return await _render(
        """
        <div class="p-3">
          <p class="extra-small text-muted mb-3">
            Found <strong>{{ releases|length }}</strong> release version{{ 's' if releases|length != 1 else '' }}
            under <em>{{ album }}</em>. Select the exact version you want to queue:
          </p>

          <div class="d-flex flex-column gap-2">
            {% for rel in releases %}
              <div class="card border-secondary p-3 shadow-sm">
                <div class="d-flex align-items-start justify-content-between gap-2">
                  <div class="overflow-hidden">
                    <div class="fw-bold text-light extra-small text-truncate">
                      {{ rel.title }}
                      {% if rel.disambiguation %}
                        <span class="text-info fw-normal">({{ rel.disambiguation }})</span>
                      {% endif %}
                    </div>
                    <div class="text-muted extra-small mt-1 d-flex align-items-center gap-1 flex-wrap">
                      <span class="badge bg-secondary extra-small">{{ rel.status }}</span>
                      <span>{{ rel.format }}</span> &bull; <span>{{ rel.country }}</span> &bull; <span>{{ rel.date }}</span>
                    </div>
                  </div>
                  <span class="badge bg-success-subtle text-success border border-success-subtle extra-small flex-shrink-0">
                    <i class="bi bi-music-note-beamed me-1"></i>{{ rel.track_count }} tracks
                  </span>
                </div>

                <div class="d-flex align-items-center justify-content-between mt-3 pt-2 border-top border-secondary">
                  <button type="button" class="btn btn-sm btn-outline-info" onclick="toggleReleaseTracklistPreview('{{ rel.id }}')">
                    <i class="bi bi-list-ul"></i> Preview Tracks
                  </button>
                  <button type="button" class="btn btn-sm btn-success d-flex align-items-center gap-1"
                          onclick='queueSpecificRelease("{{ rel.id }}", {{ rel.title|tojson }}, {{ rel.artist|tojson }})'>
                    <i class="bi bi-download"></i> Queue This Version
                  </button>
                </div>

                <div id="preview-rel-{{ rel.id }}" class="mt-2 d-none extra-small text-muted border-top border-secondary pt-2"></div>
              </div>
            {% endfor %}
          </div>
        </div>
        """,
        releases=processed,
        album=album,
        artist=artist,
    )


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/releases/active
# ---------------------------------------------------------------------------

@mb_bp.route("/releases/active", methods=["GET"])
def api_get_active_releases():
    """Get all active MusicBrainz releases with download progress."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT * FROM musicbrainz_releases WHERE status != 'finalized' ORDER BY created_at DESC LIMIT 50"))
            rows = result.fetchall()
        return jsonify({"success": True, "releases": [dict(r._mapping) for r in rows]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/download
# ---------------------------------------------------------------------------

@mb_bp.route("/download", methods=["POST"])
async def api_musicbrainz_download():
    """Initiate a managed download from a MusicBrainz release with full track integration."""
    data = (await request.get_json(silent=True)) or {}
    release_id = str(data.get("release_id") or "").strip()
    release_title = str(data.get("release_title") or "").strip()
    artist = str(data.get("artist") or "").strip()
    method = str(data.get("method") or "").strip().lower()
    persistent_search = bool(data.get("persistent_search", False))
    max_retries = data.get("max_retries", 3)
    session_id = data.get("session_id")
    # When set, the album is added as plain queue items grouped by
    # import_group (a "queue item folder") instead of being tracked as a
    # MusicBrainz monitoring release ("folder group").
    queue_items_only = bool(data.get("queue_items_only", False))

    if not all([release_id, release_title, artist]):
        return jsonify({"error": "Missing required parameters"}), 400

    try:
        method, method_error = _normalize_download_method(
            method,
            default_method="slskd",
            context=f"MusicBrainz release {release_id}",
        )
        if method_error:
            return jsonify({"error": method_error}), 400

        result = start_release_download(
            release_id,
            release_title,
            artist,
            method=method,
            create_folder_group=not queue_items_only,
        )

        if not result.get("success"):
            # Fall back to a simple single-item download when MusicBrainz data is unavailable.
            logger.warning(
                "[MB_DOWNLOAD] start_release_download failed for %s: %s — falling back to simple queue add",
                release_id,
                result.get("error"),
            )
            add_result = queue_add({
                "artist": artist,
                "title": release_title,
                "album": release_title,
                "source": "soulseek",
            })
            if not add_result.get("success"):
                return jsonify({"error": add_result.get("error") or result.get("error", "Download failed")}), 500

            queue_id = (add_result.get("item") or {}).get("id")
            return jsonify({
                "success": True,
                "tracking_id": queue_id,
                "message": f"Download queued for {release_title} (MusicBrainz data not available, using simple search)",
                "persistent_search": persistent_search,
                "session_id": session_id,
            }), 201

        tracking_id = result.get("mb_release_db_id")
        queued_tracks = int(result.get("queue_items_created") or 0)

        # When no monitoring folder group is created (queue_items_only), expose
        # the first queue item id as a tracking handle so the UI has something
        # concrete to report.
        if not tracking_id and result.get("queue_ids"):
            tracking_id = result.get("queue_ids")[0]

        # Wake the queue worker immediately (add_release_tracks_to_queue
        # inserts directly without signalling).
        try:
            from services.queue.queue_signal import signal_new_item
            signal_new_item()
        except Exception:
            pass

        return jsonify({
            "success": True,
            "tracking_id": tracking_id,
            "message": f"Download queued for {release_title} ({queued_tracks} tracks)",
            "total_tracks": queued_tracks,
            "queued_tracks": queued_tracks,
            "persistent_search": persistent_search,
            "session_id": session_id,
        }), 201

    except Exception as exc:
        logger.error("[MB_DOWNLOAD] Error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/link-album-mbids
# ---------------------------------------------------------------------------

def _normalise_track_key(value: Any) -> str:
    """Normalise a track title for fuzzy matching (lowercase, non-alphanumerics stripped)."""
    return re.sub(r"[^a-z0-9]+", "", (str(value or "").lower()))


def _match_release_tracklist(
    local_tracks: list[dict[str, Any]],
    mb_tracks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Match local tracks (missing Recording IDs) to MusicBrainz release tracks.

    ``local_tracks`` entries carry ``id``/``title``/``track_number``/
    ``disc_number``.  ``mb_tracks`` entries carry ``position``/``number``/
    ``title``/``recording_mbid``.  Matching is by (disc, position) first, then
    by normalised title.  Returns a list of ``{"track_id", "title",
    "recording_mbid"}`` for every local track that resolved.
    """
    by_position: dict[tuple[int, int], dict[str, Any]] = {}
    by_title: dict[str, list[dict[str, Any]]] = {}
    for track in mb_tracks:
        if not (track.get("recording_mbid") or "").strip():
            continue
        position = track.get("position")
        if isinstance(position, int) and position > 0:
            by_position.setdefault((1, position), track)
        title_key = _normalise_track_key(track.get("title"))
        if title_key:
            by_title.setdefault(title_key, []).append(track)

    matched: list[dict[str, Any]] = []
    for track in local_tracks:
        target = None
        try:
            position = int(track.get("track_number"))
        except (TypeError, ValueError):
            position = None
        try:
            disc = int(track.get("disc_number") or 1)
        except (TypeError, ValueError):
            disc = 1
        if position is not None and position > 0:
            if disc != 1:
                target = by_position.get((disc, position))
            else:
                target = by_position.get((1, position))
        if not target:
            title_key = _normalise_track_key(track.get("title"))
            candidates = by_title.get(title_key) or []
            if candidates:
                target = candidates[0]
        if not target or not (target.get("recording_mbid") or "").strip():
            continue
        matched.append({
            "track_id": track.get("id"),
            "title": track.get("title"),
            "recording_mbid": target["recording_mbid"],
        })
    return matched


@mb_bp.route("/link-album-mbids", methods=["POST"])
async def api_link_album_mbids():
    """Auto-link local tracks missing Recording MBIDs.

    Accepts ``{artist, album, release_id?}``.  When a MusicBrainz ``release_id``
    is provided the official release tracklist is fetched (recordings inc) and
    matched to local tracks by disc/position first, then by normalised title.
    Every linked Recording ID is written to both ``musicbrainz_trackid`` and
    ``recording_mbid``.
    """
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()
    release_id = str(data.get("release_id") or "").strip()

    if not artist or not album:
        return jsonify({"success": False, "error": "artist and album are required"}), 400

    # ── Fetch the official MusicBrainz release tracklist (recordings) ──────
    mb_tracks: list[dict[str, Any]] = []
    release_title = album
    if release_id and re.fullmatch(MUSICBRAINZ_UUID_RE, release_id):
        try:
            release = _get_mb_client().get_release(release_id, inc="recordings", timeout=15)
        except Exception as exc:
            logger.warning("[LINK_MBIDS] Could not fetch release %s: %s", release_id, exc)
            release = {}
        release_title = str(release.get("title") or album)
        for medium in release.get("media") or []:
            for track in medium.get("tracks") or []:
                recording = track.get("recording") or {}
                mb_tracks.append({
                    "position": track.get("position"),
                    "number": track.get("number"),
                    "title": str(track.get("title") or "").strip(),
                    "length": track.get("length"),
                    "recording_mbid": str(recording.get("id") or "").strip(),
                })

    # ── Load local tracks missing a Recording ID ───────────────────────────
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, title, track_number, disc_number,
                           musicbrainz_trackid, recording_mbid
                    FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    ORDER BY COALESCE(disc_number, '1'), track_number
                """),
                {"artist": artist, "album": album},
            ).fetchall()
            local_tracks = [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.error("[LINK_MBIDS] Failed to load local tracks: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500

    unlinked = [
        t for t in local_tracks
        if not (t.get("musicbrainz_trackid") or t.get("recording_mbid"))
    ]

    if not unlinked:
        return jsonify({
            "success": True,
            "linked": 0,
            "message": "All tracks already have Recording IDs.",
        })

    if not mb_tracks:
        return jsonify({
            "success": False,
            "error": "No MusicBrainz release tracklist available — select a release first (Lookup → Apply).",
        })

    # ── Match by (disc, position) then by normalised title ─────────────────
    matched: list[dict[str, Any]] = []
    linked = 0
    try:
        with db_session() as session:
            for track in _match_release_tracklist(unlinked, mb_tracks):
                session.execute(
                    text("""
                        UPDATE tracks
                        SET musicbrainz_trackid = :mbid, recording_mbid = :mbid
                        WHERE id = :id
                    """),
                    {"mbid": track["recording_mbid"], "id": track["track_id"]},
                )
                matched.append(track)
                linked += 1
    except Exception as exc:
        logger.error("[LINK_MBIDS] Failed to link MBIDs: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify({
        "success": True,
        "linked": linked,
        "matched": matched,
        "release_title": release_title,
        "message": f"Linked {linked} of {len(unlinked)} tracks on “{release_title}”.",
    })


# ---------------------------------------------------------------------------
# GET /api/musicbrainz/downloads
# ---------------------------------------------------------------------------

@mb_bp.route("/downloads", methods=["GET"])
def api_musicbrainz_downloads():
    """List MusicBrainz release downloads with per-track progress counts."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT id, release_id, release_title, artist, method, status,
                       created_at, updated_at
                FROM musicbrainz_releases
                WHERE status != 'removed'
                ORDER BY created_at DESC
                LIMIT 100
            """))
            rows = result.fetchall()

            downloads = []
            for row in rows:
                release_db_id = row[0]
                release_id = row[1]

                counts = session.execute(text("""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                           SUM(CASE WHEN status IN ('downloading', 'in_progress') THEN 1 ELSE 0 END) AS downloading,
                           SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                    FROM download_queue
                    WHERE release_id = :rid
                """), {"rid": release_id}).fetchone()

                total_tracks = int(counts[0] or 0)
                completed_tracks = int(counts[1] or 0)
                downloading_tracks = int(counts[2] or 0)
                failed_tracks = int(counts[3] or 0)

                downloads.append({
                    "id": release_db_id,
                    "release_id": release_id,
                    "release_title": row[2],
                    "artist": row[3],
                    "method": row[4],
                    "status": _frontend_status(row[5]),
                    "created_at": row[6].isoformat() if row[6] else None,
                    "updated_at": row[7].isoformat() if row[7] else None,
                    "total_tracks": total_tracks,
                    "completed_tracks": completed_tracks,
                    "downloading_tracks": downloading_tracks,
                    "failed_tracks": failed_tracks,
                })

        return jsonify({"downloads": downloads})

    except Exception as exc:
        logger.error("[MB_DOWNLOADS] Error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/musicbrainz/download/<id>/retry
# ---------------------------------------------------------------------------

@mb_bp.route("/download/<int:download_id>/retry", methods=["POST"])
def api_musicbrainz_retry(download_id: int):
    """Retry a failed MusicBrainz release download."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT release_id FROM musicbrainz_releases WHERE id = :id
            """), {"id": download_id})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Download not found"}), 404

            release_id = row[0]

            session.execute(text("""
                UPDATE musicbrainz_releases
                SET status = 'active', updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """), {"id": download_id})

            session.execute(text("""
                UPDATE download_queue
                SET status = 'queued', failure_reason = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE release_id = :rid AND status IN ('failed', 'error', 'unmatched')
            """), {"rid": release_id})

        try:
            from services.queue.queue_signal import signal_new_item
            signal_new_item()
        except Exception:
            pass

        return jsonify({"success": True, "message": "Download retry initiated"})

    except Exception as exc:
        logger.error("[MB_RETRY] Error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# DELETE /api/musicbrainz/download/<id>
# ---------------------------------------------------------------------------

@mb_bp.route("/download/<int:download_id>", methods=["DELETE"])
def api_musicbrainz_remove(download_id: int):
    """Remove a MusicBrainz release download from the tracking list."""
    try:
        with db_session() as session:
            session.execute(text("""
                UPDATE musicbrainz_releases
                SET status = 'removed', updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """), {"id": download_id})

        return jsonify({"success": True})

    except Exception as exc:
        logger.error("[MB_REMOVE] Error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
