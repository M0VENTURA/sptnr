"""Popularity provider source wrappers.

This module performs provider data acquisition and light provider-specific
normalization. It should not decide star ratings or single status.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

try:  # C-speed fuzzy matching — see _token_similarity
    from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover — stdlib fallback keeps matching working
    from difflib import SequenceMatcher as _difflib_matcher
    _HAVE_RAPIDFUZZ = False

from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import (
    get_recording_popularity_batch as lb_get_recording_popularity_batch,
    get_listenbrainz_popularity as lb_get_listenbrainz_popularity,
    get_listenbrainz_score as lb_get_listenbrainz_score,
    get_release_metadata_batch as lb_get_release_metadata_batch,
)
from helpers.normalization_service import strip_cover_attribution
from services.popularity.popularity_matching import (
    ARTIST_JOIN_RE,
    choose_best_provider_counts,
    get_artist_lookup_candidates,
    get_primary_artist_preserve_case,
    normalize_for_aggregation,
    title_variants_compatible,
)

logger = logging.getLogger(__name__)

DEFAULT_LISTENBRAINZ_BATCH_SIZE = 100
_lastfm_artist_catalog_cache: dict[str, list[dict]] = {}


def _token_similarity(a: str, b: str) -> float:
    """Title similarity on a 0-1 scale (shared ``fuzzy_match_score``)."""
    from services.popularity.popularity_math import fuzzy_match_score
    return fuzzy_match_score(a, b)


_FEATURED_ARTIST_RE = re.compile(
    r"^(.*?)\s+(?:feat\.?|ft\.?|featuring)\s+(.*)$",
    re.IGNORECASE,
)


def invert_featured_artist(artist_name: str) -> str:
    """Swap a "Primary feat. Guest" credit to "Guest feat. Primary".

    Discogs / Last.fm frequently index the feat. single under the inverted
    credit, so a failed primary lookup retries the inverted name.  Only
    ``feat.``/``ft.``/``featuring`` credits invert — ``&``/``x`` joins are
    left alone (they are real band names: "Hall & Oates").
    """
    match = _FEATURED_ARTIST_RE.match(artist_name or "")
    if not match:
        return artist_name or ""
    primary, featured = match.group(1).strip(), match.group(2).strip()
    if not primary or not featured:
        return artist_name
    return f"{featured} feat. {primary}"


def _is_featured_artist(artist_name: str) -> bool:
    return bool(_FEATURED_ARTIST_RE.match(artist_name or ""))


def resolve_isrc_recording(
    isrc: str,
    *,
    mb_client=None,
    title: str = "",
    artist: str = "",
) -> dict | None:
    """Resolve an ISRC to its MusicBrainz recording (MBID + title + artist).

    ISRCs are unique per recording, so the ``isrc/<code>`` lookup is the most
    precise key MusicBrainz offers — it bypasses every string-formatting
    issue (feat. credits, version suffixes, punctuation).  When several
    recordings share the ISRC (re-issues), the one whose title/artist match
    the local track is preferred.

    Returns ``{"recording_mbid", "title", "artist"}`` or None.
    """
    isrc = str(isrc or "").strip()
    if not isrc:
        return None
    try:
        if mb_client is None:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            mb_client = MusicBrainzHttpClient(enabled=True)
        recordings = mb_client.lookup_by_isrc(isrc) or []
        if not recordings:
            return None
        if not title and not artist:
            first = recordings[0]
            return {
                "recording_mbid": str(first.get("id") or "").strip() or None,
                "title": str(first.get("title") or "").strip(),
                "artist": _first_credit_name(first),
            }

        # Prefer the recording whose title/artist match the local track so a
        # re-issued ISRC lands on the right performance.
        target_title = normalize_for_aggregation(title)
        target_artist = str(artist or "").casefold().strip()
        best = None
        best_score = 0.0
        for rec in recordings:
            rec_title = normalize_for_aggregation(str(rec.get("title") or ""))
            rec_artist = str(_first_credit_name(rec) or "").casefold().strip()
            score = _token_similarity(target_title, rec_title)
            if target_artist and rec_artist:
                artist_hit = (
                    target_artist == rec_artist
                    or target_artist == get_primary_artist_preserve_case(rec_artist).casefold()
                )
                score = score * 0.7 + (1.0 if artist_hit else 0.0) * 0.3
            if score > best_score:
                best_score = score
                best = rec
        if best is None:
            return None
        return {
            "recording_mbid": str(best.get("id") or "").strip() or None,
            "title": str(best.get("title") or "").strip(),
            "artist": _first_credit_name(best),
        }
    except Exception as exc:
        logger.debug(
            "[ISRC_POOL] ISRC lookup failed for %s: %s", isrc, exc,
        )
        return None


def _first_credit_name(recording: dict) -> str:
    """Primary artist name from a recording's artist-credit."""
    credits = recording.get("artist-credit") or []
    for credit in credits:
        if isinstance(credit, dict):
            name = credit.get("name") or ""
            if name:
                return str(name)
        elif credit:
            return str(credit)
    return ""

# Version annotations that denote a DIFFERENT performance of the song — live
# recordings, acoustic takes, remixes, etc. have their own listen audiences
# and must never be summed into the studio track's count. Version splits of
# the SAME performance ("(Single Version)", "(Radio Edit)") are kept.
_ALTERNATE_PERFORMANCE_RE = re.compile(
    r"\([^)]*\b(?:live|unplugged|acoustic|orchestral|symphonic|demo|instrumental|"
    r"karaoke|remix|alternate|alt|take|session|rehearsal|jam[- ]along)\b[^)]*\)"
    r"|\s+-\s*(?:live|unplugged|acoustic|orchestral|symphonic|demo|instrumental|"
    r"karaoke|remix|alternate|alt|take|session|rehearsal|jam[- ]along)\s*$",
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
            score = _token_similarity(title.lower(), album.lower())
            if score > best_score:
                best_score = score
                best_mbid = str(rel.get("id") or "").strip()
        if best_mbid and best_score >= 0.8:
            logger.debug("[LB_ALBUM] Resolved release '%s - %s' via MB search -> %s", artist, album, best_mbid)
            return best_mbid
    except Exception as exc:
        logger.debug("[LB_ALBUM] Release search failed for %s - %s: %s", artist, album, exc)
    return ""


def _index_release_tracklist(
    media: list,
    titles_to_mbids: Dict[str, Dict[str, Any]],
    position_index: Dict[tuple, Dict[str, Any]],
    recording_mbids: List[str],
) -> None:
    """Index a release's media tracklists into the title/position indexes.

    Handles BOTH API shapes — MusicBrainz nests the recording under
    ``track["recording"]["id"]``; ListenBrainz release metadata uses
    ``track["recording_mbid"]`` directly.  ``titles_to_mbids`` is keyed by
    NORMALISED title; ``position_index`` by ``(disc, position)``;
    ``recording_mbids`` collects every recording for the popularity batch.
    """
    for medium in media:
        if not isinstance(medium, dict):
            continue
        disc = medium.get("position")
        for trk in medium.get("tracks") or []:
            if not isinstance(trk, dict):
                continue
            title = str(trk.get("title") or "").strip()
            if not title:
                continue
            rec = trk.get("recording") or {}
            rec_mbid = str(rec.get("id") or trk.get("recording_mbid") or "").strip()
            key = normalize_for_aggregation(title)
            entry = titles_to_mbids.setdefault(key, {
                "title": title,
                "mbids": [],
                "position": trk.get("position") or trk.get("number"),
                "disc": disc,
                "length_ms": trk.get("length"),
            })
            if rec_mbid:
                entry["mbids"].append(rec_mbid)
                recording_mbids.append(rec_mbid)
            try:
                pos = int(trk.get("position") or trk.get("number") or 0)
            except (TypeError, ValueError):
                pos = 0
            try:
                disc_i = int(disc) if disc not in (None, "") else 1
            except (TypeError, ValueError):
                disc_i = 1
            if pos > 0:
                pos_key = (disc_i, pos)
                pos_entry = position_index.setdefault(pos_key, {
                    "key": key,
                    "length_ms": trk.get("length"),
                    "mbids": [],
                })
                if rec_mbid:
                    pos_entry["mbids"].append(rec_mbid)


def get_listenbrainz_album_tracklist_with_release(
    artist: str,
    album: str,
    tracks: List[dict],
) -> tuple[Dict[str, Dict[str, Optional[int]]], str]:
    """Release-first ListenBrainz lookup for one album's tracks.

    Keys the lookup on the album's RELEASE MBID (local track columns first,
    then a MusicBrainz release search), pulls the release's OWN tracklist —
    ListenBrainz's cached release metadata first, MusicBrainz as fallback for
    releases LB has not cached — and returns per-track listen counts for the
    recordings ON this release.  Re-released songs therefore keep the listen
    count of the library's release instead of adopting another release's
    recording.  Multiple recordings of the same title are aggregated.

    Args:
        artist: artist name.
        album: album name (used for the release search fallback).
        tracks: local track dicts — optionally carry the release MBID
            (``musicbrainz_albumid`` / ``musicbrainz_album_mbid``).

    Returns:
        ``({normalized_title: {"listenbrainz_listens", "listenbrainz_users",
        "recording_mbid"}}, release_mbid)`` — the map holds only non-zero
        entries; ``release_mbid`` is "" when the album's release could not be
        resolved.
    """
    if not tracks:
        return {}, ""
    release_mbid = _resolve_release_mbid(artist, album, tracks)
    if not release_mbid:
        return {}, ""

    # Tracklist: LISTENBRAINZ's own cached view of the release first — the
    # exact tracklist (and hence the per-recording listen counts) the
    # ListenBrainz album page aggregates.  Fall back to MUSICBRAINZ (release
    # → recordings) for releases the LB metadata store has not cached (its
    # /metadata/release/ endpoint 404s for those, e.g. Phantoma); the
    # per-recording counts come from the LB popularity API in both cases.
    #
    # Tracks are also indexed by disc+position so a local track whose title
    # does NOT appear on the tracklist (e.g. the release lists the Korean
    # name "삐처리" while the library stores the English "BLEEP") can still
    # be matched by track number + length — the two rows are the same song,
    # so the album's authoritative listen count must reach the local row.
    titles_to_mbids: Dict[str, Dict[str, Any]] = {}
    recording_mbids: List[str] = []
    # (disc, position) → {"key": normalized title, "length_ms", "mbids": []}
    position_index: Dict[tuple, Dict[str, Any]] = {}
    _tracklist_source = ""
    try:
        _lb_rel = lb_get_release_metadata_batch([release_mbid]) or {}
        if isinstance(_lb_rel, dict):
            _rel_container = _lb_rel.get("releases") if isinstance(_lb_rel.get("releases"), dict) else _lb_rel
            _rel_entry = (_rel_container or {}).get(release_mbid) or {}
            _media = _rel_entry.get("media") or []
            if _media:
                _index_release_tracklist(_media, titles_to_mbids, position_index, recording_mbids)
                _tracklist_source = "listenbrainz"
    except Exception as exc:
        logger.debug("[LB_ALBUM] LB release metadata failed for %s: %s", release_mbid, exc)
    if not recording_mbids:
        try:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            mb = MusicBrainzHttpClient(enabled=True)
            release = mb.get_release(release_mbid, inc="recordings")
            _index_release_tracklist(release.get("media") or [], titles_to_mbids, position_index, recording_mbids)
            _tracklist_source = "musicbrainz"
        except Exception as exc:
            logger.debug("[LB_ALBUM] MB release tracklist failed for %s: %s", release_mbid, exc)
            return {}, release_mbid

    if not recording_mbids:
        return {}, release_mbid

    try:
        counts = lb_get_recording_popularity_batch(recording_mbids) or {}
    except Exception as exc:
        logger.debug("[LB_ALBUM] Recording popularity failed for %s: %s", release_mbid, exc)
        return {}, release_mbid

    def _sum_counts(mbids: List[str]) -> tuple[int, int]:
        total = 0
        users = 0
        for m in mbids:
            entry = counts.get(m) or {}
            total += int(entry.get("total_listen_count") or 0)
            users += int(entry.get("total_user_count") or 0)
        return total, users

    out: Dict[str, Dict[str, Optional[int]]] = {}
    for key, entry in titles_to_mbids.items():
        total, users = _sum_counts(entry["mbids"])
        if total > 0:
            out[key] = {
                "listenbrainz_listens": total,
                "listenbrainz_users": users,
                # The album's own recording for this title — lets the track
                # adopt it so LB lookups match the ListenBrainz album page.
                "recording_mbid": entry["mbids"][0] if entry["mbids"] else None,
            }

    # Position/duration fallback: local tracks whose title key got no LB data
    # (wrong recording MBID, or the album lists a translated title such as the
    # Korean "삐처리" vs the library's English "BLEEP") are matched against the
    # release tracklist by disc + track number, with a length sanity check.
    # The album tracklist is authoritative for per-track counts, so this only
    # fires when the title match was empty.
    used_pos_keys: set[tuple] = set()
    for t in tracks:
        local_title = t.get("title")
        if not local_title:
            continue
        local_key = normalize_for_aggregation(local_title)
        if not local_key:
            continue
        if (out.get(local_key) or {}).get("listenbrainz_listens"):
            continue
        try:
            local_pos = int(t.get("track_number") or 0)
        except (TypeError, ValueError):
            continue
        if local_pos <= 0:
            continue
        try:
            local_disc = int(t.get("disc_number") or 1)
        except (TypeError, ValueError):
            local_disc = 1
        pos_key = (local_disc, local_pos)
        if pos_key in used_pos_keys:
            continue
        pos_entry = position_index.get(pos_key)
        if not pos_entry or not pos_entry["mbids"]:
            continue
        # Length sanity check when both sides carry it (MB length in ms, local
        # duration in seconds) — guards against a mis-numbered album matching
        # the wrong track.
        mb_len_ms = pos_entry.get("length_ms")
        try:
            local_dur = float(t.get("duration") or 0)
        except (TypeError, ValueError):
            local_dur = 0.0
        if mb_len_ms and local_dur > 0:
            if abs(int(mb_len_ms) - local_dur * 1000) > 5000:
                continue
        total, users = _sum_counts(pos_entry["mbids"])
        if total <= 0:
            continue
        out[local_key] = {
            "listenbrainz_listens": total,
            "listenbrainz_users": users,
            "recording_mbid": pos_entry["mbids"][0],
        }
        used_pos_keys.add(pos_key)
        logger.info(
            "[LB_ALBUM] Position-matched '%s' (disc %s, track %s, %ss) -> '%s' (%s listens)",
            local_title, local_disc, local_pos, local_dur, pos_entry.get("key"), total,
        )
    if out:
        logger.info(
            "[LB_ALBUM] Preloaded %d track(s) for '%s - %s' (release %s, tracklist: %s)",
            len(out), artist, album, release_mbid, _tracklist_source,
        )
    else:
        logger.debug("[LB_ALBUM] No ListenBrainz data for '%s - %s'", artist, album)
    return out, release_mbid


def get_listenbrainz_album_tracklist(
    artist: str,
    album: str,
    tracks: List[dict],
) -> Dict[str, Dict[str, Optional[int]]]:
    """Backward-compatible wrapper — returns only the tracklist map."""
    return get_listenbrainz_album_tracklist_with_release(artist, album, tracks)[0]


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

    # Prefer the SHARED artist.getTopTracks map (populated by the bulk
    # prefetch / aggregated lookups) — derives the artist peak without a
    # second artist.getTopTracks call for the same artist.  The map is
    # primary-artist keyed; a feat. credit that misses it falls through to
    # the raw-artist call below (unchanged behaviour).
    try:
        from api_clients.lastfm import LastFmClient as _FacadeLastFmClient
        from services.popularity.popularity_cache_service import get_artist_top_tracks_map
        _map = get_artist_top_tracks_map(_FacadeLastFmClient(api_key=api_key), artist) or {}
        _peak = max((int(e.get("lastfm_listeners") or 0) for e in _map.values()), default=0)
        if _peak > 0:
            _lastfm_artist_max_cache[artist_key] = _peak
            return _peak
    except Exception:
        pass

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


def get_aggregated_lastfm_popularity(
    artist: str,
    track_title: str,
    lastfm_client=None,
    isrc: str | None = None,
    recording_mbid: str | None = None,
) -> dict:
    """Aggregate Last.fm counts across locally matched title variants.

    Featured-artist tracks are handled specially: the album version (usually
    the low-listen one) is what ``artist.getTopTracks`` and ``track.getInfo``
    return, while the single version carries the real popularity.  For those
    tracks we search Last.fm for every published version of the song and keep
    the higher combined count.

    When the primary (artist + title) lookups come back empty, two fallback
    arms run and the MAX wins (best-of-three):
      1. ISRC arm — resolve the ISRC to its MusicBrainz recording and query
         ``track.getInfo`` by recording MBID.  ISRCs bypass every
         string-formatting issue (feat. credits, version suffixes, dots).
      2. Inverted-arm — "Primary feat. Guest" is re-queried as
         "Guest feat. Primary"; Last.fm often only indexes the feat. single
         under the inverted credit.
    """
    if lastfm_client is None:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}
    # Strip cover attributions so the Last.fm query targets the canonical
    # track row ("Gangnam Style (PSY Cover)" → "Gangnam Style"), otherwise the
    # low-listen "(Cover)" album row is returned instead of the real single.
    track_title = strip_cover_attribution(track_title) or track_title
    is_featured = (
        "feat" in str(artist or "").casefold()
        or "feat" in str(track_title or "").casefold()
    )
    # Key the artist catalogue by the PRIMARY artist (feat. suffix stripped) so
    # "dArtagnan feat. X" tracks share the "dArtagnan" catalogue instead of
    # issuing a wasted artist.getTopTracks call for a name Last.fm does not know.
    primary_artist = get_primary_artist_preserve_case(artist)
    artist_key = primary_artist.casefold().strip()
    catalog = []
    try:
        # Prefer the SHARED artist.getTopTracks map (primary-artist keyed,
        # limit 200, populated by the bulk prefetch) — the dedicated fetch
        # below used to issue a SECOND artist.getTopTracks call for the same
        # artist.  Map values are variant-SUMMED per normalized title, which
        # matches what the prefetch fast-path already scores with.  Fall back
        # to the raw list when the map is empty.
        from services.popularity.popularity_cache_service import get_artist_top_tracks_map
        _map = get_artist_top_tracks_map(lastfm_client, primary_artist) or {}
        catalog = [
            {
                "name": key,
                "listeners": int(e.get("lastfm_listeners") or 0),
                "playcount": int(e.get("lastfm_playcount") or 0),
            }
            for key, e in _map.items()
            if e.get("lastfm_listeners")
        ]
    except Exception:
        catalog = []
    if not catalog and artist_key not in _lastfm_artist_catalog_cache and hasattr(lastfm_client, "get_artist_top_tracks"):
        try:
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

    # ── Collab / multi-artist split ───────────────────────────────────────
    # "BABYMETAL & Electric Callboy" (or "A x B", "A and B") indexes the same
    # track separately under EACH artist's Last.fm catalogue — scrobbles get
    # split across the two artist rows, so querying only the primary artist
    # returns a fraction of the real popularity (RATATATA showed 1.1k LF on
    # the Electric Callboy side while the track is one of the biggest metal
    # songs of 2024).  When the primary catalogue finds nothing, split the
    # credit and query each sub-artist's catalogue for the same title, merging
    # the counts so the collab's real audience is recovered.
    if not matched and ARTIST_JOIN_RE.search(artist or ""):
        collab_parts = [p.strip() for p in ARTIST_JOIN_RE.split(artist or "") if p.strip()]
        if len(collab_parts) >= 2:
            for part in collab_parts:
                try:
                    from services.popularity.popularity_cache_service import get_artist_top_tracks_map
                    _part_map = get_artist_top_tracks_map(lastfm_client, part) or {}
                    _part_catalog = [
                        {
                            "name": key,
                            "listeners": int(e.get("lastfm_listeners") or 0),
                            "playcount": int(e.get("lastfm_playcount") or 0),
                        }
                        for key, e in _part_map.items()
                        if e.get("lastfm_listeners")
                    ]
                    if not _part_catalog:
                        _key = part.casefold().strip()
                        if _key not in _lastfm_artist_catalog_cache and hasattr(lastfm_client, "get_artist_top_tracks"):
                            _lastfm_artist_catalog_cache[_key] = lastfm_client.get_artist_top_tracks(part)
                            _part_catalog = _lastfm_artist_catalog_cache.get(_key, [])
                    for item in _part_catalog or []:
                        item_title = item.get("name") or item.get("title") or ""
                        if normalize_for_aggregation(item_title) == target:
                            matched.append(item)
                            listeners += int(item.get("listeners", 0) or 0)
                            playcount += int(item.get("playcount", item.get("track_play", 0)) or 0)
                except Exception:
                    continue

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
        primary = {
            "listeners": int(info.get("listeners", 0) or 0),
            "track_play": int(info.get("track_play", 0) or 0),
            "matched_tracks": [],
        }
    except Exception:
        primary = {"listeners": 0, "track_play": 0, "matched_tracks": []}

    # ── ISRC + inverted-artist fallback arms (best-of-three) ──────────────
    # Only worth the extra API calls when the primary lookup produced
    # nothing (or the credit is a feat. split that Last.fm only indexes
    # under the inverted artist name).
    variants: dict[str, tuple[int, int]] = {"Primary": (primary["listeners"], primary["track_play"])}
    best = primary
    best_key = "Primary"

    def _consider(arm: str, stats: dict) -> None:
        nonlocal best, best_key
        listeners = int((stats or {}).get("listeners") or 0)
        playcount = int((stats or {}).get("track_play") or (stats or {}).get("playcount") or 0)
        variants[arm] = (listeners, playcount)
        if listeners > best["listeners"] or (listeners == best["listeners"] and playcount > best["track_play"]):
            best = {"listeners": listeners, "track_play": playcount, "matched_tracks": []}
            best_key = arm

    need_fallback = (
        best["listeners"] == 0 and best["track_play"] == 0
    ) or _is_featured_artist(artist)
    if need_fallback:
        # 1. ISRC arm: ISRC → MusicBrainz recording → track.getInfo by MBID.
        if isrc or recording_mbid:
            try:
                _isrc_rec = None
                if isrc:
                    _isrc_rec = resolve_isrc_recording(
                        isrc, title=track_title, artist=artist,
                    )
                _arm_mbid = (
                    (_isrc_rec or {}).get("recording_mbid") or recording_mbid
                )
                _arm_artist = (_isrc_rec or {}).get("artist") or artist
                _arm_title = (_isrc_rec or {}).get("title") or track_title
                if _arm_mbid:
                    _isrc_stats = lastfm_client.get_track_info(
                        _arm_artist, _arm_title, track_mbid=_arm_mbid,
                    )
                    _consider("ISRC", _isrc_stats)
            except Exception as exc:
                logger.debug("[ISRC_POOL] Last.fm ISRC arm failed for %s: %s", isrc or recording_mbid, exc)

        # 2. Inverted arm: "Primary feat. Guest" → "Guest feat. Primary".
        if _is_featured_artist(artist):
            try:
                inverted = invert_featured_artist(artist)
                if inverted != artist:
                    _inv_stats = lastfm_client.get_track_info(inverted, track_title)
                    _consider("Inverted", _inv_stats)
            except Exception as exc:
                logger.debug("[ISRC_POOL] Last.fm inverted arm failed for %s: %s", artist, exc)

    if best_key != "Primary":
        _detail = {k: v[0] for k, v in variants.items() if v[0] > 0}
        logger.info(
            "[POPULARITY] Queried %d variant(s) -> Max listeners: %d (%s)",
            len(variants), best["listeners"],
            " | ".join(f"{k}: {v:,}" for k, v in _detail.items()),
        )
        return {
            "listeners": best["listeners"],
            "track_play": best["track_play"],
            "matched_tracks": best["matched_tracks"],
            "sources_queried": len(variants),
            "variant_detail": _detail,
        }
    return best


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
    # Strip cover attributions before searching so the API is queried with the
    # canonical title ("Gangnam Style", not "Gangnam Style (PSY Cover)").
    track_title = strip_cover_attribution(track_title) or track_title
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
            # A hard version marker on one side but not the other means a
            # DIFFERENT performance — "(Live)"/"(Acoustic)"/"(Instrumental)/
            # (Orchestral)"/"(Remix)"/"(Demo)" must never inherit the
            # canonical track's popularity (token_set_ratio treats
            # "see you in hell acoustic" as a perfect superset of
            # "see you in hell", so without this gate a live cut is scored
            # with the studio recording's 25k+ listeners).
            if not title_variants_compatible(track_title, item_title):
                continue
            # Only keep versions of THIS song — normalised title comparison
            # correlates "Herzblut", "Herzblut (feat. Melissa Bonny)", etc.
            # A rapidfuzz fallback (token_set_ratio >= 90) catches residual
            # split-variant drift the normalizer misses ("Song (Mix)" vs
            # "Song", apostrophe/punctuation drift) without ever merging two
            # different songs.
            _item_key = normalize_for_aggregation(item_title)
            if _item_key != target and _token_similarity(_item_key, target) < 0.90:
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
    isrc: Optional[str] = None,
    lb_client=None,
    mb_client=None,
) -> dict:
    """Aggregate ListenBrainz stats across split MBIDs when possible.

    Searches MusicBrainz for every recording of the track by the same artist
    (single vs album versions are separate recordings) and sums their
    ListenBrainz listen counts.  When the track has an ISRC but no recording
    MBID, the ISRC resolves the recording first — the most precise key
    available, bypassing string matching entirely.
    """
    logger.debug("[POPULARITY_SOURCES] Fetching aggregated ListenBrainz popularity")
    mbids: set[str] = set()
    if primary_mbid:
        mbids.add(primary_mbid)
    if isrc and not primary_mbid:
        try:
            _isrc_rec = resolve_isrc_recording(isrc, title=title, artist=artist)
            _isrc_mbid = (_isrc_rec or {}).get("recording_mbid")
            if _isrc_mbid:
                mbids.add(_isrc_mbid)
                logger.debug(
                    "[ISRC_POOL] LB aggregation resolved ISRC %s -> recording %s",
                    isrc, _isrc_mbid,
                )
        except Exception:
            pass
    if mb_client is None:
        try:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            mb_client = MusicBrainzHttpClient()
        except Exception:
            mb_client = None
    if mb_client and hasattr(mb_client, "search_recordings"):
        try:
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
                if norm_rec == norm_target or _token_similarity(norm_rec, norm_target) >= 0.85:
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


def _recording_work_mbid(recording: dict) -> str:
    """Work MBID a recording performs, from its ``work-rels`` relations.

    MusicBrainz links a recording to the song it performs via a
    ``performance`` relation whose target is a Work entity::

        {"relations": [{"type": "performance", "work": {"id": "…", "title": "…"}}]}

    Returns "" when the recording carries no Work link.
    """
    for rel in recording.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        if str(rel.get("type") or "").lower() != "performance":
            continue
        work = rel.get("work") or {}
        wid = str(work.get("id") or "").strip()
        if wid:
            return wid
    return ""


def _recording_artist_mbids(recording: dict) -> set[str]:
    """Set of artist MBIDs credited on a recording (``artist-credit``)."""
    out: set[str] = set()
    for credit in recording.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        art = credit.get("artist") or {}
        aid = str(art.get("id") or "").strip()
        if aid:
            out.add(aid)
    return out


def _recording_primary_artist(recording: dict) -> str:
    """Primary (first) artist name credited on a recording."""
    for credit in recording.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        art = credit.get("artist") or {}
        name = str(art.get("name") or credit.get("name") or "").strip()
        if name:
            return name
    return ""


def _empty_work_lb_result() -> dict:
    return {
        "total_listen_count": 0,
        "total_user_count": 0,
        "mbids": [],
        "work_mbid": "",
        "source": "work",
    }


def get_work_level_listenbrainz_popularity(
    title: str,
    artist: str,
    artist_mbid: str = "",
    primary_mbid: str = "",
    isrc: str = "",
    lb_client=None,
    mb_client=None,
) -> dict:
    """Aggregate ListenBrainz counts across a single MusicBrainz Work.

    When a song is released as a single, its scrobbles on ListenBrainz get
    splintered across multiple ``recording_mbid``s that all perform the SAME
    Work — the 7" single edit, the album cut, a Greatest Hits remaster, a
    radio promo.  Querying only the recording pinned to the library's album
    release picks up a tiny fraction of the real traffic.

    Resolution chain (per the single-detection design):
      1. Resolve the track's ``work_mbid`` from its recording (``work-rels``)
         or its ISRC when no recording MBID is stored.
      2. Browse every recording linked to that Work (one throttled call).
      3. Keep only recordings by the SAME artist — the Work links every
         recording of the song including COVERS by other artists, which must
         never inflate the count (strict ``artist_mbid`` filter when the
         local artist MBID is known, else normalized name equality).
      4. Sum the ListenBrainz listen/user counts across the kept recordings.

    Args:
        title: Local track title.
        artist: Local track artist.
        artist_mbid: Local artist MBID (strict artist filter — the cover-song
            trap guard).  Empty falls back to normalized name equality.
        primary_mbid: The track's recording MBID (seeded for the Work lookup).
        isrc: Track ISRC (used when no recording MBID is available).
        lb_client / mb_client: Optional clients (tests inject fakes).

    Returns:
        ``{total_listen_count, total_user_count, mbids, work_mbid, source}``
        with ``source == "work"``; zero counts when no Work is resolvable or
        no same-artist recordings are found.
    """
    logger.debug("[POPULARITY_SOURCES] Fetching Work-level aggregated ListenBrainz popularity")
    if mb_client is None:
        try:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            mb_client = MusicBrainzHttpClient(enabled=True)
        except Exception:
            mb_client = None
    if mb_client is None:
        return _empty_work_lb_result()

    # Seed the Work lookup from the track's recording MBID, or resolve the
    # recording from the ISRC (most precise key when no MBID is stored).
    seed_mbids: set[str] = set()
    if primary_mbid:
        seed_mbids.add(primary_mbid)
    elif isrc:
        try:
            _isrc_rec = resolve_isrc_recording(isrc, title=title, artist=artist, mb_client=mb_client)
            if _isrc_rec and _isrc_rec.get("recording_mbid"):
                seed_mbids.add(_isrc_rec["recording_mbid"])
        except Exception:
            pass

    # 1. Resolve the Work from the seed recording's work-rels.
    work_mbid = ""
    for seed_mbid in seed_mbids:
        try:
            rec = mb_client.get_recording(seed_mbid, inc="work-rels+artist-credits")
            if not rec or not rec.get("id"):
                continue
            work_mbid = _recording_work_mbid(rec)
            if work_mbid:
                break
        except Exception as exc:
            logger.debug("[WORK_LB] Recording work-rels fetch failed for %s: %s", seed_mbid, exc)

    if not work_mbid:
        logger.debug(
            "[WORK_LB] No Work resolvable for '%s - %s' (mbids=%s)",
            artist, title, sorted(seed_mbids),
        )
        return _empty_work_lb_result()

    # 2. Browse every recording of the Work.
    recordings: list[dict] = []
    try:
        recordings = mb_client.browse_work_recordings(work_mbid, inc="artist-credits", limit=100) or []
    except Exception as exc:
        logger.debug("[WORK_LB] Work recording browse failed for %s: %s", work_mbid, exc)

    # 3. Keep only same-artist studio recordings (no covers / live / remix).
    mbids: set[str] = set(seed_mbids)
    # Strip the featured-guest credit before the name comparison — MB credits a
    # feat. track's recording to the PRIMARY artist ("Feuerschwanz feat. Dag von
    # SDP" records under "Feuerschwanz"), so without this the name filter
    # excluded every browsed recording and work-level aggregation never fired.
    from helpers.normalization_service import strip_featured_artist
    target_artist = normalize_for_aggregation(strip_featured_artist(artist) or artist)
    for rec in recordings:
        if not isinstance(rec, dict):
            continue
        rec_id = str(rec.get("id") or "").strip()
        rec_title = str(rec.get("title") or "")
        if not rec_id or not rec_title:
            continue
        if _is_alternate_performance_title(rec_title):
            continue
        rec_artist_mbids = _recording_artist_mbids(rec)
        if artist_mbid:
            # Strict artist-MBID filter — the "cover song" trap: a Work
            # links every recording of the song, including covers.
            if artist_mbid not in rec_artist_mbids:
                continue
        else:
            rec_artist = _recording_primary_artist(rec)
            if not rec_artist or normalize_for_aggregation(rec_artist) != target_artist:
                continue
        mbids.add(rec_id)

    if not mbids:
        return _empty_work_lb_result()

    # 4. Sum ListenBrainz counts across the same-artist recordings.
    try:
        batch = lb_get_recording_popularity_batch(list(mbids))
        listen_count = sum(int((batch.get(mbid) or {}).get("total_listen_count") or 0) for mbid in mbids)
        user_count = sum(int((batch.get(mbid) or {}).get("total_user_count") or 0) for mbid in mbids)
        logger.debug(
            "[WORK_LB] Aggregated LB for '%s - %s' (work %s): %s listens / %s users across %d recording(s) %s",
            artist, title, work_mbid, listen_count, user_count, len(mbids), sorted(mbids),
        )
        return {
            "total_listen_count": listen_count,
            "total_user_count": user_count,
            "mbids": sorted(mbids),
            "work_mbid": work_mbid,
            "source": "work",
        }
    except Exception:
        logger.debug("[WORK_LB] Aggregated LB failed for '%s - %s'", artist, title, exc_info=True)
        return _empty_work_lb_result()


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
