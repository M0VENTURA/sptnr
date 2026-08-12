"""Artist context helpers used by advanced single detection."""
from __future__ import annotations
from statistics import mean, stdev


def get_artist_listenbrainz_context(artist_mbid: str) -> dict:
    """Return the artist's ListenBrainz top-10% listen threshold.

    Uses the current ``ListenBrainzClient.get_top_recordings_for_artist``
    (the old module-level ``get_artist_recordings_popularity`` no longer
    exists, which previously made this helper silently return empty).
    """
    if not artist_mbid:
        return {"recordings": [], "threshold": 0, "total": 0}
    try:
        from api_clients.listenbrainz import ListenBrainzClient
        recordings = ListenBrainzClient().get_top_recordings_for_artist(artist_mbid) or []
    except Exception:
        recordings = []
    counts = sorted([int(r.get("total_listen_count") or 0) for r in recordings], reverse=True)
    threshold = counts[max(0, int(len(counts) * 0.10) - 1)] if counts else 0
    return {"recordings": recordings, "threshold": threshold, "total": len(counts)}


def blend_top_10_thresholds(lastfm_threshold: int, lastfm_total: int, listenbrainz_threshold: int, listenbrainz_total: int) -> tuple[int, str]:
    if lastfm_total and listenbrainz_total:
        return int((lastfm_threshold + listenbrainz_threshold) / 2), "blended"
    if lastfm_total:
        return int(lastfm_threshold), "lastfm"
    if listenbrainz_total:
        return int(listenbrainz_threshold), "listenbrainz"
    return 0, "none"


def get_artist_lastfm_context(artist_name: str, conn, artist_mbid: str | None = None) -> dict:
    """Return the artist's Last.fm listener distribution from cached tracks.

    ``conn`` is kept for backward compatibility — the query runs on its own
    SQLAlchemy session.
    """
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text(
                    "SELECT title, lastfm_listeners FROM tracks "
                    "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND lastfm_listeners > 0"
                ),
                {"artist": artist_name},
            ).fetchall() or []
        values = [int(row[1] or 0) for row in rows]
        return {"mean": mean(values) if values else 0, "stdev": stdev(values) if len(values) > 1 else 0, "total": len(values), "values": values}
    except Exception:
        return {"mean": 0, "stdev": 0, "total": 0, "values": []}


def get_dynamic_lastfm_weight(artist_context: dict, track_lastfm_listeners: int, base_lastfm_weight: float = 0.3) -> float:
    avg = artist_context.get("mean", 0) or 0
    sd = artist_context.get("stdev", 0) or 0
    if not avg or not sd:
        return base_lastfm_weight
    z = (track_lastfm_listeners - avg) / sd
    if z >= 2:
        return min(0.7, base_lastfm_weight + 0.25)
    if z <= -1:
        return max(0.1, base_lastfm_weight - 0.15)
    return base_lastfm_weight

