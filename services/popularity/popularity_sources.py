"""Popularity provider source wrappers.

This module performs provider data acquisition and light provider-specific
normalization. It should not decide star ratings or single status.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import (
    get_recording_popularity_batch as lb_get_recording_popularity_batch,
    get_listenbrainz_popularity as lb_get_listenbrainz_popularity,
    get_listenbrainz_score as lb_get_listenbrainz_score,
)
from services.popularity.popularity_matching import (
    choose_best_provider_counts,
    get_artist_lookup_candidates,
    get_primary_artist_preserve_case,
    normalize_for_aggregation,
)

logger = logging.getLogger(__name__)

DEFAULT_LISTENBRAINZ_BATCH_SIZE = 100
_lastfm_artist_catalog_cache: dict[str, list[dict]] = {}

# Version annotations that denote a DIFFERENT performance of the song — live
# recordings, acoustic takes, remixes, etc. have their own listen audiences
# and must never be summed into the studio track's count. Version splits of
# the SAME performance ("(Single Version)", "(Radio Edit)") are kept.
_ALTERNATE_PERFORMANCE_RE = re.compile(
    r"\([^)]*\b(?:live|unplugged|acoustic|orchestral|symphonic|demo|instrumental|"
    r"karaoke|remix|alternate|alt|take|session|rehearsal)\b[^)]*\)"
    r"|\s+-\s*(?:live|unplugged|acoustic|orchestral|symphonic|demo|instrumental|"
    r"karaoke|remix|alternate|alt|take|session|rehearsal)\s*$",
    re.IGNORECASE,
)


def _is_alternate_performance_title(rec_title: str) -> bool:
    """True when a recording title is a different performance (live/remix/…).

    Markers are only matched inside parentheses or as a dash suffix so plain
    song titles containing the word (e.g. "Live and Let Die") are kept.
    """
    return bool(_ALTERNATE_PERFORMANCE_RE.search(rec_title or ""))


def extract_recording_mbid(track: dict) -> Optional[str]:
    """Return recording MBID suitable for ListenBrainz popularity calls."""
    return track.get("recording_mbid") or track.get("musicbrainz_recording_mbid") or track.get("mbid")


def get_listenbrainz_batch_for_tracks(tracks: List[dict]) -> Dict[str, Dict[str, Optional[int]]]:
    """Fetch ListenBrainz popularity in chunks for track dicts."""
    mbids = [extract_recording_mbid(track) for track in tracks]
    mbids = [mbid for mbid in mbids if mbid]
    output: Dict[str, Dict[str, Optional[int]]] = {}
    for index in range(0, len(mbids), DEFAULT_LISTENBRAINZ_BATCH_SIZE):
        chunk = mbids[index:index + DEFAULT_LISTENBRAINZ_BATCH_SIZE]
        try:
            output.update(lb_get_recording_popularity_batch(chunk))
        except Exception as exc:
            logger.debug("ListenBrainz batch lookup failed: %s", exc)
    return output


def _resolve_release_mbid(artist: str, album: str, tracks: List[dict]) -> str:
    """Resolve the album's release MBID without depending on local tracks.

    Priority:
      1. Local track columns (``musicbrainz_albumid`` / ``musicbrainz_album_mbid``).
      2. MusicBrainz release search by artist + album (artist-credit checked,
         title similarity >= 0.8).

    Returns "" when unresolvable.
    """
    for t in tracks:
        mbid = str(t.get("musicbrainz_albumid") or t.get("musicbrainz_album_mbid") or "").strip()
        if mbid:
            return mbid
    try:
        from difflib import SequenceMatcher
        from api_clients.musicbrainz_http import MusicBrainzHttpClient, escape_lucene_special_chars
        client = MusicBrainzHttpClient(enabled=True)
        query = (
            f'artist:"{escape_lucene_special_chars(artist)}" '
            f'AND release:"{escape_lucene_special_chars(album)}"'
        )
        releases = client.search_releases(query, limit=5) or []
        artist_norm = _normalize_artist(artist)
        best_mbid = ""
        best_score = 0.0
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            title = str(rel.get("title") or "").strip()
            credits = rel.get("artist-credit") or []
            names = []
            for credit in credits:
                if isinstance(credit, dict):
                    art = credit.get("artist") or {}
                    names.append(art.get("name") or credit.get("name") or "")
            if names and not any(_normalize_artist(n) == artist_norm for n in names):
                continue
            score = SequenceMatcher(None, title.lower(), album.lower()).ratio()
            if score > best_score:
                best_score = score
                best_mbid = str(rel.get("id") or "").strip()
        if best_mbid and best_score >= 0.8:
            logger.debug("[LB_ALBUM] Resolved release '%s - %s' via MB search -> %s", artist, album, best_mbid)
            return best_mbid
    except Exception as exc:
        logger.debug("[LB_ALBUM] Release search failed for %s - %s: %s", artist, album, exc)
    return ""


def get_listenbrainz_album_tracklist(
    artist: str,
    album: str,
    tracks: List[dict],
) -> Dict[str, Dict[str, Optional[int]]]:
    """Fetch an album's per-track ListenBrainz popularity matched by title.

    ListenBrainz lists albums with their tracks, so albums whose local tracks
    lack recording MBIDs can still get LB listen counts: resolve the album's
    release MBID (local tracks first, then a MusicBrainz release search),
    fetch the release tracklist, batch the popularity for those recordings,
    and key the result by normalized title.  Multiple recordings of the same
    title are aggregated.

    Args:
        artist: artist name.
        album: album name (used for the release search fallback).
        tracks: local track dicts — optionally carry the release MBID
            (``musicbrainz_albumid`` / ``musicbrainz_album_mbid``).

    Returns ``{normalized_title: {"listenbrainz_listens", "listenbrainz_users"}}``
    with only non-zero entries.
    """
    if not tracks:
        return {}
    release_mbid = _resolve_release_mbid(artist, album, tracks)
    if not release_mbid:
        return {}

    try:
        from api_clients.listenbrainz import ListenBrainzClient
        lb = ListenBrainzClient()
        meta = lb.get_release_metadata_batch([release_mbid]) or {}
        release_meta = (meta.get(release_mbid) or {}).get("release") or {}
    except Exception as exc:
        logger.debug("[LB_ALBUM] Release metadata failed for %s: %s", release_mbid, exc)
        return {}

    # Flatten the tracklist: normalized title -> recording MBIDs.
    titles_to_mbids: Dict[str, List[str]] = {}
    recording_mbids: List[str] = []
    for medium in release_meta.get("media") or []:
        if not isinstance(medium, dict):
            continue
        for trk in medium.get("tracks") or []:
            if not isinstance(trk, dict):
                continue
            title = str(trk.get("title") or "").strip()
            if not title:
                continue
            key = normalize_for_aggregation(title)
            rec_mbid = str(trk.get("recording_mbid") or "").strip()
            titles_to_mbids.setdefault(key, [])
            if rec_mbid:
                titles_to_mbids[key].append(rec_mbid)
                recording_mbids.append(rec_mbid)

    if not recording_mbids:
        return {}

    try:
        counts = lb_get_recording_popularity_batch(recording_mbids) or {}
    except Exception as exc:
        logger.debug("[LB_ALBUM] Recording popularity failed for %s: %s", release_mbid, exc)
        return {}

    out: Dict[str, Dict[str, Optional[int]]] = {}
    for key, mbids in titles_to_mbids.items():
        total = 0
        users = 0
        for m in mbids:
            entry = counts.get(m) or {}
            total += int(entry.get("total_listen_count") or 0)
            users += int(entry.get("total_user_count") or 0)
        if total > 0:
            out[key] = {
                "listenbrainz_listens": total,
                "listenbrainz_users": users,
                # The album's own recording for this title — lets the track
                # adopt it so LB lookups match the ListenBrainz album page.
                "recording_mbid": mbids[0] if mbids else None,
            }
    if out:
        logger.info(
            "[LB_ALBUM] Preloaded %d track(s) for '%s - %s' (release %s)",
            len(out), artist, album, release_mbid,
        )
    else:
        logger.debug("[LB_ALBUM] No ListenBrainz data for '%s - %s'", artist, album)
    return out


def get_listenbrainz_popularity_for_track(track: dict) -> Dict[str, Optional[int]]:
    """Fetch ListenBrainz popularity for one track dict."""
    mbid = extract_recording_mbid(track)
    if not mbid:
        return {"total_listen_count": None, "total_user_count": None}
    try:
        return lb_get_listenbrainz_popularity(mbid)
    except Exception:
        return {"total_listen_count": None, "total_user_count": None}


def get_listenbrainz_score_for_track(track: dict) -> int:
    """Backward-compatible per-track ListenBrainz raw listen count."""
    mbid = extract_recording_mbid(track)
    if not mbid:
        return 0
    try:
        return int(lb_get_listenbrainz_score(mbid) or 0)
    except Exception:
        return 0


def get_lastfm_track_info(
    artist: str,
    title: str,
    track_mbid: str | None = None,
    album_artist: str | None = None,
    lastfm_client: LastFmClient | None = None,
) -> dict:
    """Fetch Last.fm track info using conservative artist candidates."""
    if lastfm_client is None:
        return {"track_play": 0, "listeners": 0}
    results = []
    for candidate in get_artist_lookup_candidates(artist, album_artist=album_artist):
        try:
            results.append(lastfm_client.get_track_info(candidate, title, track_mbid=track_mbid))
        except Exception:
            continue
    return choose_best_provider_counts(results) if results else {"track_play": 0, "listeners": 0}


# Module-level cache: artist_key (casefold) → max listener count from top tracks.
_lastfm_artist_max_cache: dict[str, int] = {}


def get_lastfm_artist_max_listeners(
    artist: str,
    api_key: str | None = None,
) -> int:
    """Fetch the peak listener count across an artist's top tracks on Last.fm.

    Results are cached in-memory so repeated calls for the same artist
    (across multiple albums) cost at most one API request.

    Returns 0 if Last.fm is not configured or the artist has no data.
    """
    if not api_key:
        from helpers.config_helpers import get_config
        cfg = get_config()
        api_key = cfg.get("api_integrations", {}).get("lastfm", {}).get("api_key", "")
    if not api_key:
        return 0

    artist_key = artist.casefold().strip()
    cached = _lastfm_artist_max_cache.get(artist_key)
    if cached is not None:
        return cached

    from api_clients.lastfm_http import LastFmHttpClient
    client = LastFmHttpClient(api_key=api_key)

    try:
        data = client.get_json(
            "artist.getTopTracks",
            timeout=(5, 10),
            artist=artist,
            limit=100,
        )
        if not data or "error" in data:
            _lastfm_artist_max_cache[artist_key] = 0
            return 0

        tracks = data.get("toptracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]

        max_listeners = 0
        for track in tracks:
            if isinstance(track, dict):
                try:
                    listeners = int(track.get("listeners", 0) or 0)
                    if listeners > max_listeners:
                        max_listeners = listeners
                except (ValueError, TypeError):
                    continue

        _lastfm_artist_max_cache[artist_key] = max_listeners
        return max_listeners
    except Exception as exc:
        logger.debug("Failed to get Last.fm top tracks for '%s': %s", artist, exc)
        _lastfm_artist_max_cache[artist_key] = 0
        return 0


def get_aggregated_lastfm_popularity(artist: str, track_title: str, lastfm_client=None) -> dict:
    """Aggregate Last.fm counts across locally matched title variants.

    Featured-artist tracks are handled specially: the album version (usually
    the low-listen one) is what ``artist.getTopTracks`` and ``track.getInfo``
    return, while the single version carries the real popularity.  For those
    tracks we search Last.fm for every published version of the song and keep
    the higher combined count.
    """
    if lastfm_client is None:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}
    is_featured = (
        "feat" in str(artist or "").casefold()
        or "feat" in str(track_title or "").casefold()
    )
    # Key the artist catalogue by the PRIMARY artist (feat. suffix stripped) so
    # "dArtagnan feat. X" tracks share the "dArtagnan" catalogue instead of
    # issuing a wasted artist.getTopTracks call for a name Last.fm does not know.
    primary_artist = get_primary_artist_preserve_case(artist)
    artist_key = primary_artist.casefold().strip()
    try:
        if artist_key not in _lastfm_artist_catalog_cache and hasattr(lastfm_client, "get_artist_top_tracks"):
            _lastfm_artist_catalog_cache[artist_key] = lastfm_client.get_artist_top_tracks(primary_artist)
        catalog = _lastfm_artist_catalog_cache.get(artist_key, [])
    except Exception:
        catalog = []

    target = normalize_for_aggregation(track_title)
    matched = []
    listeners = 0
    playcount = 0
    for item in catalog or []:
        item_title = item.get("name") or item.get("title") or ""
        if normalize_for_aggregation(item_title) == target:
            matched.append(item)
            listeners += int(item.get("listeners", 0) or 0)
            playcount += int(item.get("playcount", item.get("track_play", 0)) or 0)

    # Featured tracks: search for ALL versions (album + feat. single) and keep
    # the higher combined count so a low-listen album match can't shadow the
    # single's real popularity.
    if is_featured or not matched:
        search = get_search_aggregated_lastfm_popularity(artist, track_title, lastfm_client=lastfm_client)
        search_listeners = int(search.get("listeners") or 0)
        if search_listeners > listeners:
            listeners = search_listeners
            playcount = int(search.get("track_play") or 0)
            matched = search.get("matched_tracks") or matched

    if matched:
        return {"listeners": listeners, "track_play": playcount, "matched_tracks": matched}
    try:
        info = lastfm_client.get_track_info(artist, track_title)
        return {"listeners": int(info.get("listeners", 0) or 0), "track_play": int(info.get("track_play", 0) or 0), "matched_tracks": []}
    except Exception:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}


def get_search_aggregated_lastfm_popularity(
    artist: str,
    track_title: str,
    lastfm_client=None,
) -> dict:
    """Aggregate Last.fm counts across every published version of a song.

    The album version and the single / featured version of a song are separate
    Last.fm tracks with separate listener counts, and ``artist.getTopTracks``
    only surfaces the few that chart — the low-listen album version is often
    what ``track.getInfo`` returns while the high-listen single sits on a
    different track row.  This searches Last.fm by the main artist name plus
    the track title (as the user's album-scan single detection does) and sums
    the counts of every matching version of the same song.

    Returns ``{"listeners", "track_play", "matched_tracks"}``.
    """
    if lastfm_client is None:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}
    target = normalize_for_aggregation(track_title)
    if not target:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}

    matched: list[dict] = []
    seen: set[str] = set()

    def _collect(candidate: str) -> None:
        try:
            results = lastfm_client.search_track(candidate, track_title, limit=20)
        except Exception:
            results = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            item_title = str(item.get("name") or item.get("title") or "")
            if not item_title:
                continue
            # Only keep versions of THIS song — normalised title comparison
            # correlates "Herzblut", "Herzblut (feat. Melissa Bonny)", etc.
            if normalize_for_aggregation(item_title) != target:
                continue
            url = str(item.get("url") or "").strip()
            key = url or f"{str(item.get('artist') or '').casefold()}::{item_title.casefold()}"
            if key in seen:
                continue
            seen.add(key)
            matched.append(item)

    # Search the PRIMARY artist first — "dArtagnan" + "Herzblut" surfaces the
    # feat. single rows in one call — then the remaining candidates (e.g. the
    # full "dArtagnan feat. Melissa Bonny" string) for any version only
    # indexed under the full artist credit.  Results de-dupe by URL.
    primary = get_primary_artist_preserve_case(artist)
    ordered_candidates = [primary] + [
        c for c in get_artist_lookup_candidates(artist)
        if c.casefold() != primary.casefold()
    ]
    for candidate in ordered_candidates:
        _collect(candidate)

    listeners = sum(int(item.get("listeners") or 0) for item in matched)
    playcount = sum(
        int(item.get("playcount") or item.get("track_play") or 0)
        for item in matched
    )
    if matched:
        logger.info(
            "[LF_SEARCH] Aggregated %s version(s) of '%s - %s' -> %s listeners / %s plays",
            len(matched), artist, track_title, listeners, playcount,
        )
        return {"listeners": listeners, "track_play": playcount, "matched_tracks": matched}

    # Nothing searchable — fall back to the direct single-track lookup.
    try:
        info = lastfm_client.get_track_info(artist, track_title)
        return {
            "listeners": int(info.get("listeners") or 0),
            "track_play": int(info.get("track_play") or 0),
            "matched_tracks": [],
        }
    except Exception:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}


def get_aggregated_listenbrainz_popularity(
    title: str,
    artist: str,
    primary_mbid: Optional[str] = None,
    lb_client=None,
    mb_client=None,
) -> dict:
    """Aggregate ListenBrainz stats across split MBIDs when possible.

    Searches MusicBrainz for every recording of the track by the same artist
    (single vs album versions are separate recordings) and sums their
    ListenBrainz listen counts.
    """
    logger.debug("[POPULARITY_SOURCES] Fetching aggregated ListenBrainz popularity")
    mbids: set[str] = set()
    if primary_mbid:
        mbids.add(primary_mbid)
    if mb_client is None:
        try:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            mb_client = MusicBrainzHttpClient()
        except Exception:
            mb_client = None
    if mb_client and hasattr(mb_client, "search_recordings"):
        try:
            from difflib import SequenceMatcher as _SM
            from helpers.normalization_service import (
                normalize_title_for_lucene_query,
                normalize_title_for_lookup,
                strip_single_release_suffix,
            )
            from api_clients.musicbrainz_http import escape_lucene_special_chars
            query = (
                f'recording:"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}" '
                f'AND artist:"{escape_lucene_special_chars(artist)}"'
            )
            norm_target = normalize_title_for_lookup(strip_single_release_suffix(title) or title)
            for rec in mb_client.search_recordings(query, limit=20):
                rec_id = rec.get("id")
                rec_title = str(rec.get("title") or "")
                if not rec_id or not rec_title:
                    continue
                # Only sum recordings of the SAME studio performance — live,
                # acoustic, remix and alternate takes are separate
                # performances and must not inflate the track's count.
                if _is_alternate_performance_title(rec_title):
                    continue
                norm_rec = normalize_title_for_lookup(strip_single_release_suffix(rec_title) or rec_title)
                if norm_rec == norm_target or _SM(None, norm_rec, norm_target).ratio() >= 0.85:
                    mbids.add(rec_id)
        except Exception:
            pass
    if not mbids:
        return {"total_listen_count": 0, "total_user_count": 0, "mbids": []}
    try:
        batch = lb_get_recording_popularity_batch(list(mbids))
        listen_count = sum(int((batch.get(mbid) or {}).get("total_listen_count") or 0) for mbid in mbids)
        user_count = sum(int((batch.get(mbid) or {}).get("total_user_count") or 0) for mbid in mbids)
        logger.debug(
            "[POPULARITY_SOURCES] Aggregated LB for '%s - %s': %s listens / %s users across %d recording(s) %s",
            artist, title, listen_count, user_count, len(mbids), sorted(mbids),
        )
        return {"total_listen_count": listen_count, "total_user_count": user_count, "mbids": sorted(mbids)}
    except Exception:
        logger.debug(
            "[POPULARITY_SOURCES] Aggregated LB failed for '%s - %s'", artist, title, exc_info=True,
        )
        return {"total_listen_count": 0, "total_user_count": 0, "mbids": sorted(mbids)}


def get_metadata_sources_info(single_sources: list[str]) -> dict:
    """Extract metadata-source flags from a list of source identifiers.

    Args:
        single_sources: List of source names (e.g. ``["discogs", "spotify"]``).

    Returns:
        Dict with boolean flags for each source and a display-ready source list.
    """
    has_discogs = "discogs" in single_sources or "discogs_video" in single_sources
    has_spotify = "spotify" in single_sources
    has_musicbrainz = "musicbrainz" in single_sources
    has_lastfm = "lastfm" in single_sources
    has_version_count = "version_count" in single_sources

    has_metadata = has_discogs or has_spotify or has_musicbrainz or has_lastfm

    sources_list: list[str] = []
    if has_discogs:
        sources_list.append("Discogs")
    if has_spotify:
        sources_list.append("Spotify")
    if has_musicbrainz:
        sources_list.append("MusicBrainz")
    if has_lastfm:
        sources_list.append("Last.fm")
    if has_version_count:
        sources_list.append("Version Count")

    return {
        "has_discogs": has_discogs,
        "has_spotify": has_spotify,
        "has_musicbrainz": has_musicbrainz,
        "has_lastfm": has_lastfm,
        "has_version_count": has_version_count,
        "has_metadata": has_metadata,
        "sources_list": sources_list,
    }
