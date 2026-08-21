"""
MusicBrainz enrichment service.

Owns MusicBrainz interpretation/business rules:
- title/version normalisation
- single detection
- release-group matching
- release suggestion extraction
- relationship interpretation
- clean-name lookup

✅ No DB writes
✅ No track mutation
✅ Pure enrichment + lookup
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, List, Dict, Tuple

try:  # C-speed fuzzy matching — see _similarity
    from rapidfuzz import fuzz as _rapidfuzz_fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover — stdlib fallback keeps matching working
    import difflib as _difflib
    _HAVE_RAPIDFUZZ = False

from api_clients.musicbrainz_http import (
    MUSICBRAINZ_UUID_RE,
    MusicBrainzHttpClient,
    escape_lucene_special_chars,
)

from helpers.normalization_service import (
    normalize_string,
    normalize_title_for_lookup,
    normalize_title_for_lucene_query,
    normalize_title_for_mbid_match,
    strip_featured_artist,
    strip_single_release_suffix,
    strip_search_keywords,
    edition_annotations_compatible,
)

logger = logging.getLogger(__name__)


# Library-track query used by ``compare_musicbrainz_release``.  Kept as a
# module constant so tests can assert it stays portable across SQLite (test
# runs) and PostgreSQL (production): ``disc_number`` / ``track_number`` are
# TEXT columns, so COALESCE must use string literals — integer literals make
# PostgreSQL raise "COALESCE types text and integer cannot be matched",
# which was silently swallowed and surfaced as the misleading "No library
# tracks found for this album".
_COMPARE_LIBRARY_TRACKS_SQL = """
    SELECT id, title, track_number, disc_number, artist, year,
           mbid, file_path, duration, mb_ignored_fields
    FROM tracks
    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
      AND LOWER(COALESCE(album, '')) = LOWER(:album)
    ORDER BY COALESCE(disc_number, '1'), COALESCE(track_number, '999')
"""


def _similarity(a: str, b: str) -> float:
    """String similarity on a 0-1 scale (shared ``fuzzy_match_score``)."""
    from services.popularity.popularity_math import fuzzy_match_score
    return fuzzy_match_score(a, b)


def _mbid_similarity(a: str, b: str) -> float:
    """Order-insensitive title similarity WITHOUT subset inflation.

    ``token_set_ratio`` treats "Valhalla" as a 1.0 subset match of "Valhalla
    (Epic Edition)", which makes MBID resolution ambiguous — a studio track
    could tie with its live/epic edition recording and resolve to whichever
    MusicBrainz returns first.  Recording MBID selection needs precision, so
    ``token_sort_ratio`` (word-order insensitive, subset-penalising) is used
    there; single-detection title matching uses ``_similarity`` where subset
    matches are the desired outcome.  ``difflib`` fallback mirrors the legacy
    ``SequenceMatcher`` ratio.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _HAVE_RAPIDFUZZ:
        return _rapidfuzz_fuzz.token_sort_ratio(a, b) / 100.0
    return _difflib.SequenceMatcher(None, a, b).ratio()

CACHE_FILE = "/tmp/mbid_cache.json" if os.path.exists("/tmp") else "mbid_cache.json"

# Serialises reads/writes of the shared on-disk mbid cache — several
# MusicBrainzService instances may exist at once (album batch, per-track
# fallbacks) and concurrent scan threads must not interleave file IO.
_CACHE_IO_LOCK = threading.Lock()

# Entries per batched MusicBrainz search (``lookup_album_metadata``).  Each
# entry contributes a Lucene OR-group to the query — beyond ~20 the URL grows
# unwieldy and the results get diluted across too many candidates.
_MB_BATCH_CHUNK = 20

# Minimum recording-title similarity for a batched match to be accepted.
# The batch resolver picks each track's best candidate from ONE shared,
# truncated result set (the OR query's top-N across ALL tracks in the chunk);
# without a floor a track whose own recording was starved out of that pool can
# resolve to a WRONG sibling recording (inheriting its ISRC / ListenBrainz
# counts) — the "State bleed" / wrong-ISRC reports from #887.  Below the floor
# the track is left unmatched so the per-track lookup decides (never a wrong
# sibling).  0.6 keeps the remaster-anchor case ("Last of Us (2018 Version)"
# → the plain "Last of Us" recording, sim ≈ 0.606) while rejecting live /
# jam-along / alternate-cuts vs their studio sibling (sim ≈ 0.43-0.55).
_MB_BATCH_SIMILARITY_FLOOR = 0.6


# =============================================================================
# HELPERS
# =============================================================================


def build_artist_credit_string(artist_credit):
    """Build a display string from a MusicBrainz artist-credit array."""
    result = ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            result += credit.get('name', '')
            result += credit.get('joinphrase', '')
        else:
            result += str(credit)
    return result.strip()


def primary_album_artist(artist_credit):
    """Return the PRIMARY album artist from a MusicBrainz artist-credit.

    A release's ``artist-credit`` can credit MULTIPLE artists joined by
    phrases ("Weezer", " & ", "Rivers Cuomo").  For the ALBUM ARTIST tag that
    string must be the FIRST credit's name only — writing the joined string
    ("Weezer & Rivers Cuomo") makes Navidrome split the album because it
    treats it as two album artists.  The full multi-artist credit belongs on
    the TRACK ARTIST (per-track), never the album artist.
    """
    if isinstance(artist_credit, list) and artist_credit:
        first = artist_credit[0]
        if isinstance(first, dict):
            return str(first.get("name") or "").strip()
        return str(first or "").strip()
    if isinstance(artist_credit, str):
        return artist_credit.strip()
    return ""


def calculate_match_score(mb_title, mb_artist_credit, local_album, local_artist) -> float:
    title_sim = _similarity(normalize_string(local_album), normalize_string(mb_title))

    artist_name = ""
    if isinstance(mb_artist_credit, list) and mb_artist_credit:
        if isinstance(mb_artist_credit[0], dict):
            artist_name = mb_artist_credit[0].get("name", "")
    elif isinstance(mb_artist_credit, str):
        artist_name = mb_artist_credit

    artist_sim = _similarity(normalize_string(local_artist), normalize_string(artist_name))

    return (title_sim * 0.6) + (artist_sim * 0.4)


def _parse_secondary_types(raw) -> list[str]:
    """Normalise the MusicBrainz ``secondary-types`` field into a list.

    The search API returns secondary types as a comma-joined string (e.g.
    ``"live"`` or ``"live,compilation"``); treat a list payload defensively.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    return []


def _artist_lookup_candidates(artist: str) -> list[str]:
    """Full credit first, then the feat.-stripped primary artist.

    MusicBrainz has no artist named "Feuerschwanz feat. Dag von SDP" — the
    single is credited to "Feuerschwanz".  Dedupes case-insensitively so a
    plain artist name yields a single candidate.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for candidate in (artist or "", strip_featured_artist(artist or "")):
        key = (candidate or "").casefold().strip()
        if candidate and key not in seen:
            candidates.append(candidate)
            seen.add(key)
    return candidates


# =============================================================================
# MAIN SERVICE
# =============================================================================

# Process-wide shared MusicBrainz HTTP client (one throttle/rate-limit state,
# one HTTP connection pool).  The per-track scan path constructs a
# ``MusicBrainzService`` for every track — sharing the underlying client
# avoids tens of thousands of throwaway sessions per scan.
_SHARED_MB_CLIENT: MusicBrainzHttpClient | None = None


def get_shared_mb_client() -> MusicBrainzHttpClient:
    """Return the process-wide shared ``MusicBrainzHttpClient`` singleton."""
    global _SHARED_MB_CLIENT
    if _SHARED_MB_CLIENT is None:
        _SHARED_MB_CLIENT = MusicBrainzHttpClient(enabled=True)
    return _SHARED_MB_CLIENT


def _recording_matches_album(recording: dict, album: str) -> bool:
    """True when a recording's embedded releases include the scanned album.

    A version / alternate-take / bonus track ("Last Of Us (2018 Version)")
    must resolve to the recording that actually appears on the album being
    scanned: MusicBrainz assigns the SAME recording MBID to a remastered
    reissue (the version track IS the original recording) and a DIFFERENT
    recording to a full rerecording — so anchoring on the album resolves
    both cases from MusicBrainz data alone, without guessing whether a
    "(2018 Version)" marker means "remaster" or "rerecord".

    Both the release title and its release-group title are checked (search
    docs embed both); the match is fuzzy so a local folder name like
    "Last Of Us" still matches a release titled "Last of Us" or
    "Last Of Us (Deluxe)".
    """
    if not album or not recording:
        return False
    album = str(album).strip().lower()
    if not album:
        return False
    for release in recording.get("releases") or []:
        candidates = [release.get("title") or ""]
        rg = release.get("release-group") or {}
        if rg.get("title"):
            candidates.append(rg["title"])
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if candidate and _similarity(candidate, album) >= 0.6:
                return True
    return False


def _first_isrc(recording: dict) -> str | None:
    """First ISRC from a MusicBrainz recording or search document.

    Both the JSON search docs and the recording entity expose ISRCs as an
    ``isrcs`` array; a defensive ``isrc-list`` alias covers older payloads.
    The value is normalized to a bare 12-char code (see ``normalize_isrc``)
    so a wrapped ``{A/B}``-style tag list never leaks downstream.
    """
    from helpers.normalization_service import normalize_isrc
    isrcs = recording.get("isrcs") or recording.get("isrc-list") or []
    if isinstance(isrcs, list):
        for raw in isrcs:
            value = normalize_isrc(raw)
            if value:
                return value
    value = normalize_isrc(recording.get("isrc"))
    return value or None


class MusicBrainzService:

    def __init__(self, http_client: MusicBrainzHttpClient | None = None, enabled: bool = True):
        self.enabled = enabled
        self.http = http_client or MusicBrainzHttpClient(enabled=enabled)
        self._artist_singles_cache: dict[str, list[dict[str, Any]]] = {}
        self._mbid_cache = self._load_cache()

    # -----------------------------------------------------------------------------
    # CACHE
    # -----------------------------------------------------------------------------

    def _load_cache(self) -> dict:
        # Multiple MusicBrainzService instances (per-scan batch, per-track
        # fallbacks) share the SAME disk file — concurrent scan threads could
        # interleave read/write and corrupt it.  The lock serialises file IO
        # only; in-memory dicts stay per-instance.
        with _CACHE_IO_LOCK:
            try:
                if os.path.exists(CACHE_FILE):
                    with open(CACHE_FILE, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    # Purge poisoned entries: earlier versions cached failed
                    # lookups as ["", 0.0], which permanently disabled single
                    # detection for that track. Only keep entries with a real MBID.
                    if isinstance(raw, dict):
                        return {
                            key: value
                            for key, value in raw.items()
                            if isinstance(value, (list, tuple))
                            and len(value) == 2
                            and str(value[0] or "").strip()
                        }
            except Exception:
                pass
            return {}

    def _save_cache(self):
        with _CACHE_IO_LOCK:
            try:
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(self._mbid_cache, f)
            except Exception:
                pass

    def _cache_key(self, title, artist):
        return f"{artist.lower()}::{title.lower()}"

    # -----------------------------------------------------------------------------
    # CORE LOOKUPS
    # -----------------------------------------------------------------------------

    def get_suggested_mbid(self, title: str, artist: str, limit: int = 5) -> Tuple[str, float]:
        if not self.enabled:
            return "", 0.0

        cache_key = self._cache_key(title, artist)

        cached = self._mbid_cache.get(cache_key)
        if isinstance(cached, (list, tuple)) and len(cached) == 2 and str(cached[0] or "").strip():
            return tuple(cached)

        # Punctuation in titles ("What's the Deal?") breaks Lucene phrase
        # matching — the recording index is tokenised without punctuation.
        # Normalise the query title the same way, and compare candidates
        # against a BRACKET-PRESERVING normalisation so both sides are
        # punctuation-consistent *and* version-tagged.  Using
        # ``normalize_title_for_lookup`` here stripped "(Live)"/"(Acoustic)"
        # from BOTH the query title and the candidates, so a live bonus track
        # tied with its studio version and resolved to whichever recording
        # MusicBrainz returned first (usually the studio one) — leaking the
        # studio MBID's ListenBrainz counts onto the alternate take.
        # Configurable Search Filters (search.strip_keywords) still strip
        # same-song cuts like "(Radio Edit)" / "(Remastered)" here.
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = f'recording:"{escape_lucene_special_chars(query_title)}" AND artist:"{escape_lucene_special_chars(artist)}"'

        try:
            recordings = self.http.search_recordings(query, limit=limit)

            best_mbid = ""
            best_score = 0.0
            norm_title = normalize_title_for_mbid_match(title)

            for rec in recordings:
                rec_title = str(rec.get("title") or "")
                # A candidate whose edition annotation differs from the
                # track's is a DIFFERENT edition — never the match (a
                # "(Epic Edition)" cut must not resolve to the plain
                # recording).  Live/remaster/version markers are not
                # editions and never gate here.
                if not edition_annotations_compatible(title, rec_title):
                    continue
                # Compare both sides BRACKET-PRESERVING: normalising the
                # candidate with ``normalize_title_for_lookup`` stripped
                # "(Live)"/"(Acoustic)"/"(2018 Version)" off the candidate
                # too, so a version-tagged track tied with its plain studio
                # sibling and resolved to whichever recording MusicBrainz
                # returned first — leaking that recording's ListenBrainz
                # counts onto the alternate take.
                sim = _mbid_similarity(
                    norm_title, normalize_title_for_mbid_match(rec_title)
                )

                if sim > best_score:
                    best_score = sim
                    best_mbid = rec.get("id", "")

            result = (best_mbid, round(best_score, 3))
            # Never persist empty results — a failed lookup (rate limit,
            # transient error, no match) would otherwise poison the cache for
            # the lifetime of /tmp/mbid_cache.json and permanently disable
            # single detection for that track.
            if best_mbid:
                self._mbid_cache[cache_key] = result
                self._save_cache()

            logger.debug(
                "[MB_LOOKUP] '%s - %s' → mbid=%s (sim=%.3f, %d candidate(s))",
                artist, title, best_mbid or "-", best_score, len(recordings),
            )
            return result
        except Exception:
            logger.debug("[MB_LOOKUP] Search failed for '%s - %s'", artist, title, exc_info=True)
            return "", 0.0

    def lookup_recording_metadata(self, title: str, artist: str) -> Dict[str, Any]:
        if not title or not artist:
            return {}

        try:
            mbid, confidence = self.get_suggested_mbid(title, artist)

            if not mbid:
                return {}

            recording = self.http.get_recording(mbid, inc="artist-credits+releases")

            if not recording:
                return {}

            return self._recording_to_metadata(recording, mbid, confidence)

        except Exception as e:
            logger.debug("[MB LOOKUP] %s", e, exc_info=True)
            return {}

    def _recording_to_metadata(self, recording: dict, mbid: str, confidence: float) -> Dict[str, Any]:
        """Project a raw MusicBrainz recording document onto the metadata dict.

        Shared by the per-track lookup (``lookup_recording_metadata``) and the
        album batch (``lookup_album_metadata``) so both paths produce
        identical output shapes.
        """
        credits = recording.get("artist-credit", [])
        rec_artist = ""
        rec_artist_mbid = ""

        if credits:
            first = credits[0]
            rec_artist = first.get("name") if isinstance(first, dict) else str(first)
            if isinstance(first, dict):
                rec_artist_mbid = (
                    (first.get("artist") or {}).get("id")
                    if isinstance(first.get("artist"), dict)
                    else ""
                ) or ""

        release = (recording.get("releases") or [None])[0]

        return {
            "title": recording.get("title"),
            "artist": rec_artist,
            "artist_mbid": rec_artist_mbid or None,
            "album": release.get("title") if release else None,
            "album_artist": (
                release.get("artist-credit", [{}])[0].get("name")
                if release and release.get("artist-credit")
                else None
            ),
            "isrc": _first_isrc(recording),
            "year": (
                int(release.get("date")[:4])
                if release and release.get("date")
                else None
            ),
            "recording_mbid": mbid,
            "confidence": confidence,
        }

    def lookup_album_metadata(
        self,
        entries: list[tuple[str, str]],
        candidates_per_entry: int = 5,
        album: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """Batch MusicBrainz metadata for many tracks in ONE search per chunk.

        Builds a single Lucene OR query over ``(recording:"<title>" AND
        artist:"<artist>")`` groups (chunked to keep URL length sane) and
        scores each hit with the same bracket-preserving similarity used by
        ``get_suggested_mbid``.  Metadata — album, year, artist credit,
        recording MBID — comes straight from the search documents, replacing
        the per-track (search + recording lookup) request pair with a single
        batched search.

        ``album`` (the release being scanned) anchors version / alternate-take
        / bonus tracks ("Last Of Us (2018 Version)") to the recording that
        actually appears on that album — a remastered reissue reuses the
        original recording, a full rerecording carries its own — so the tie
        between a version track and its plain studio sibling resolves to the
        ALBUM's recording instead of whichever MusicBrainz returned first.

        Returns ``{artist.lower()::title.lower(): metadata}`` for matched
        entries only; unmatched entries fall back to the per-track lookup.
        Resolved MBIDs are written to the persistent mbid cache so later
        scans skip the search entirely.
        """
        if not self.enabled:
            return {}
        album = str(album or "").strip()
        try:
            unique = sorted(
                {
                    (str(t or "").strip(), str(a or "").strip())
                    for t, a in (entries or [])
                    if t and a
                }
            )
            if not unique:
                return {}

            results: Dict[str, Dict[str, Any]] = {}
            for chunk_start in range(0, len(unique), _MB_BATCH_CHUNK):
                chunk = unique[chunk_start:chunk_start + _MB_BATCH_CHUNK]
                groups = [
                    (
                        f'(recording:"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}" '
                        f'AND artist:"{escape_lucene_special_chars(artist)}")'
                    )
                    for title, artist in chunk
                ]
                try:
                    recordings = self.http.search_recordings(
                        " OR ".join(groups),
                        limit=min(100, len(chunk) * candidates_per_entry),
                    )
                except Exception:
                    logger.debug("[MB_LOOKUP] Album batch search failed for chunk %d", chunk_start, exc_info=True)
                    continue

                for title, artist in chunk:
                    norm_title = normalize_title_for_mbid_match(title)
                    best = None
                    best_score = 0.0
                    best_album_anchor = False
                    for rec in recordings:
                        rec_title = str(rec.get("title") or "")
                        # A candidate whose edition annotation differs from
                        # the track's is a DIFFERENT edition — never the
                        # match.  Version/live/remaster markers are not
                        # editions and never gate here.
                        if not edition_annotations_compatible(title, rec_title):
                            continue
                        # Bracket-preserving BOTH sides (see get_suggested_mbid):
                        # bracket-stripping the candidate made a version track
                        # tie with its plain sibling and resolve to whichever
                        # MusicBrainz returned first — the reported wrong-ISRC
                        # bug for "(2018 Version)" / alternate-take bonus tracks.
                        sim = _mbid_similarity(
                            norm_title, normalize_title_for_mbid_match(rec_title)
                        )
                        if sim <= 0:
                            continue
                        # The scanned album is the tie-breaker: when two
                        # recordings score the same title similarity, the one
                        # that actually appears on this album wins (remaster →
                        # same recording, rerecording → its own).
                        album_anchor = _recording_matches_album(rec, album)
                        if (
                            sim > best_score
                            or (
                                sim == best_score
                                and album_anchor
                                and not best_album_anchor
                            )
                        ):
                            best_score = sim
                            best = rec
                            best_album_anchor = album_anchor
                    mbid = (best or {}).get("id", "")
                    # Similarity floor: the best candidate in the shared pool
                    # must be a confident title match.  A track whose true
                    # recording was pushed out of the truncated result set by
                    # other tracks' candidates would otherwise resolve to the
                    # best WRONG sibling in the pool — inheriting that
                    # recording's ISRC and ListenBrainz counts.  Below the
                    # floor the track stays unmatched and falls back to the
                    # per-track lookup (which queries its own focused search).
                    if not best or not mbid or best_score < _MB_BATCH_SIMILARITY_FLOOR:
                        continue
                    confidence = round(best_score, 3)
                    results[self._cache_key(title, artist)] = self._recording_to_metadata(best, mbid, confidence)
                    self._mbid_cache[self._cache_key(title, artist)] = (mbid, confidence)
                if self._mbid_cache:
                    self._save_cache()
            return results
        except Exception:
            logger.debug("[MB_LOOKUP] Album batch failed for %d entry(s)", len(entries or []), exc_info=True)
            return {}

    def is_single(self, title: str, artist: str, album_track_count: int | None = None) -> bool:
        """Check if a track is a single using MusicBrainz release-group type.

        A track is a single when any release it appears on belongs to a
        Single/EP release-group. Three passes, in order of reliability:

        1. Recording search — results embed each recording's releases with
           their release-groups, which surfaces the single release even when
           the recording lookup truncates the embedded release list.
        2. Recording lookup — the suggested recording's releases (fallback
           when search embeds are sparse).
        3. Release-group search — the RG index tokenises punctuation
           differently (phrase queries miss "What's the Deal?"), so when the
           phrase query finds nothing, retry with an artist-scoped search and
           match by title similarity.

        Featured-artist credits ("Feuerschwanz feat. Dag von SDP") are not
        MusicBrainz artist names — every pass retries with the feat.-stripped
        primary artist so a single credited to "Feuerschwanz" alone is still
        found (e.g. "Knightclub" is a single by Feuerschwanz, not by
        "Feuerschwanz feat. Dag von SDP").
        """
        if not self.enabled or not title or not artist:
            return False
        try:
            for lookup_artist in _artist_lookup_candidates(artist):
                if self._recording_search_has_single_release(title, lookup_artist):
                    return True

                mbid, _confidence = self.get_suggested_mbid(title, lookup_artist)
                if mbid and self._recording_has_single_release(mbid, title=title):
                    return True

                if self._release_group_has_single_release(title, lookup_artist):
                    return True
            return False
        except Exception as exc:
            logger.debug("MusicBrainz is_single failed for %s / %s: %s", artist, title, exc)
            return False

    def _release_group_has_single_release(self, title: str, artist: str) -> bool:
        """True when the artist has a Single/EP release-group titled like the track.

        Phrase queries tokenise punctuation differently (they miss "What's
        the Deal?"), so when the ``releasegroup:`` query finds nothing the
        search falls back to an artist-scoped query matched by title
        similarity.  A generous limit keeps large discographies (50+ release
        groups) from hiding the matching single beyond the first page.
        """
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        rg_query = (
            f'releasegroup:"{escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        groups = self.http.search_release_groups(rg_query, limit=10)
        if not groups:
            groups = self.http.search_release_groups(
                f'artist:"{escape_lucene_special_chars(artist)}"',
                limit=50,
            )
        norm_title = normalize_title_for_lookup(title)
        for group in groups:
            pt = (
                group.get("primary-type")
                or group.get("primary_type")
                or group.get("type")
                or ""
            ).lower()
            if pt not in ("single", "ep"):
                continue
            # Edition-annotated track ("Valhalla (Epic Edition)") must only
            # match a single/EP release-group carrying the SAME edition
            # annotation — never the plain "Valhalla" single.
            if not edition_annotations_compatible(title, group.get("title") or ""):
                continue
            sim = _similarity(
                norm_title,
                normalize_title_for_lookup(group.get("title") or ""),
            )
            if sim >= 0.7:
                return True
        return False

    def _recording_search_has_single_release(self, title: str, artist: str) -> bool:
        """True when a recording-search result embeds a Single/EP release-group.

        The WS/2 recording lookup truncates the embedded ``releases`` list, but
        search results carry each recording's releases (with release-groups),
        so scan those directly before falling back to a lookup.

        The release-group title must ALSO match the track title — a track that
        merely appears on a single as a b-side ("Out on Patrol" on the "I'll
        Be Waiting" single) is NOT itself a single.
        """
        query_title = normalize_title_for_lucene_query(strip_search_keywords(title))
        query = (
            f'recording:"{escape_lucene_special_chars(query_title)}" '
            f'AND artist:"{escape_lucene_special_chars(artist)}"'
        )
        for rec in self.http.search_recordings(query, limit=10):
            for release in rec.get("releases") or []:
                rg = release.get("release-group") or {}
                pt = (
                    rg.get("primary-type")
                    or rg.get("primary_type")
                    or rg.get("type")
                    or ""
                ).lower()
                if pt not in ("single", "ep"):
                    continue
                # The single/EP release-group must be FOR this track, not just
                # contain it (b-sides live on singles too).
                if self._rg_title_matches(title, rg.get("title") or ""):
                    return True
        return False

    def _rg_title_matches(self, title: str, rg_title: str) -> bool:
        """True when a release-group title matches the track title.

        Exact normalized equality first, then a similarity fallback for
        residual punctuation/case drift. An edition-annotated track
        ("Valhalla (Epic Edition)") must only match a release-group carrying
        the SAME edition annotation — never the plain "Valhalla" single.
        """
        if not title or not rg_title:
            return False
        if not edition_annotations_compatible(title, rg_title):
            return False
        norm_title = normalize_title_for_lookup(strip_single_release_suffix(title) or title)
        norm_rg = normalize_title_for_lookup(strip_single_release_suffix(rg_title) or rg_title)
        if norm_rg == norm_title:
            return True
        return _similarity(norm_rg, norm_title) >= 0.85

    def _recording_has_single_release(self, mbid: str, title: str = "") -> bool:
        """True when any release of the recording belongs to a Single/EP
        release-group whose title matches the track title.

        Without the title check, b-sides ("Out on Patrol" on the "I'll Be
        Waiting" single) are falsely detected as singles.
        """
        recording = self.http.get_recording(
            mbid,
            inc="releases+release-groups",
            timeout=10.0,
        )
        if not recording:
            return False
        for release in recording.get("releases") or []:
            rg = release.get("release-group") or {}
            pt = (rg.get("primary-type") or rg.get("primary_type") or "").lower()
            rt = (rg.get("type") or "").lower()
            if pt not in ("single", "ep") and rt not in ("single", "ep"):
                continue
            if not title or self._rg_title_matches(title, rg.get("title") or ""):
                return True
        return False

    # -----------------------------------------------------------------------------
    # SIMPLE LOOKUPS
    # -----------------------------------------------------------------------------

    def get_artist_country(self, artist: str) -> str:
        if not self.enabled or not artist:
            return ""

        try:
            result = self.http.search_artists(
                f'artist:"{escape_lucene_special_chars(artist)}"',
                limit=1,
                inc="area",
            )

            if not result:
                return ""

            data = result[0]
            return (
                (data.get("area") or {}).get("name")
                or (data.get("begin-area") or {}).get("name")
                or ""
            )
        except Exception:
            return ""

    def get_genres(self, title: str, artist: str) -> List[str]:
        if not self.enabled:
            return []

        query = f'recording:"{escape_lucene_special_chars(strip_search_keywords(title))}" AND artist:"{escape_lucene_special_chars(artist)}"'

        try:
            recordings = self.http.search_recordings(query, limit=1, inc="tags+releases")

            if not recordings:
                return []

            tags = recordings[0].get("tags") or []
            return [t["name"] for t in tags if t.get("name")]

        except Exception:
            return []

    # -----------------------------------------------------------------------------
    # MATCHING / RELEASE HELPERS
    # -----------------------------------------------------------------------------

    def search_releasegroup_matches(self, artist_name: str, album_name: str, limit: int = 10) -> list:
        if not artist_name or not album_name:
            return []

        clean_album = strip_search_keywords(album_name)
        query = f'artist:"{escape_lucene_special_chars(artist_name)}" AND releasegroup:"{escape_lucene_special_chars(clean_album)}"'

        try:
            groups = self.http.search_release_groups(query, limit=limit)
        except Exception:
            groups = []

        # Punctuation-heavy titles ("GOLDEN HOUR: Part.4") fail the QUOTED
        # phrase — MusicBrainz's index tokenises the colon/spacing differently
        # from the stored value ("GOLDEN HOUR : Part.4"), so the phrase returns
        # zero even though the release-group exists.  Fall back to an UNQUOTED
        # term query (all terms ANDed), which the exact release-group ranks
        # first; ``calculate_match_score`` below re-scores locally so the exact
        # title wins.
        if not groups and clean_album:
            terms = normalize_title_for_lucene_query(clean_album)
            if terms:
                try:
                    groups = self.http.search_release_groups(
                        f'artist:"{escape_lucene_special_chars(artist_name)}" AND releasegroup:{terms}',
                        limit=limit,
                    )
                except Exception:
                    groups = []

        matches = []

        for group in groups:
            score = calculate_match_score(
                group.get("title"),
                group.get("artist-credit"),
                album_name,
                artist_name,
            )

            matches.append({
                "id": group.get("id"),
                "title": group.get("title"),
                "primary_type": group.get("primary-type"),
                "match_score": round(score, 3),
                # Secondary types refine the primary type (e.g. a primary
                # "album" that is secondary "live").  The search API returns
                # them as a comma-joined string; normalise to a list.
                "secondary_types": _parse_secondary_types(group.get("secondary-types")),
            })

        matches.sort(key=lambda x: x["match_score"], reverse=True)

        return matches

    # -----------------------------------------------------------------------------
    # MERGE HELPER
    # -----------------------------------------------------------------------------

    def merge_metadata(self, base: dict, mb: dict, overrides: dict | None = None) -> dict:
        overrides = overrides or {}

        def pick(*values):
            for v in values:
                if v:
                    return v
            return None

        return {
            "title": pick(overrides.get("title"), mb.get("title"), base.get("title")),
            "artist": pick(overrides.get("artist"), mb.get("artist"), base.get("artist")),
            "album": pick(overrides.get("album"), mb.get("album"), base.get("album")),
            "album_artist": pick(
                overrides.get("album_artist"),
                mb.get("album_artist"),
                base.get("album_artist"),
            ),
            "year": pick(overrides.get("year"), mb.get("year"), base.get("year")),
        }
    
    # ------------------------------------------------------------------
    # Relationship lookups (similar artists, collaborators)
    # ------------------------------------------------------------------

    def get_artist_relationships(self, artist_mbid: str, relation_type: str = "artist") -> list[dict[str, Any]]:
        """Fetch relationships for an artist (e.g. similar artists, collaborators).

        Args:
            artist_mbid: MusicBrainz artist ID.
            relation_type: Relationship direction — ``"artist"`` for artist-artist
                relationships (similar artists, collaboration).

        Returns:
            List of relationship dicts with ``type``, ``direction``, and target entity.
        """
        if not self.enabled or not artist_mbid:
            return []

        inc_map = {
            "artist": "artist-rels",
            "recording": "recording-rels",
            "work": "work-rels",
        }
        inc = inc_map.get(relation_type, "artist-rels")

        try:
            data = self.http.get_artist(artist_mbid, inc=inc)
            return data.get("relations", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch relationships for artist %s: %s", artist_mbid, exc)
            return []

    def get_recording_relationships(self, recording_mbid: str) -> list[dict[str, Any]]:
        """Fetch relationships for a recording (writers, producers, engineers).

        Args:
            recording_mbid: MusicBrainz recording ID.

        Returns:
            List of relationship dicts including work-level relationships.
        """
        if not self.enabled or not recording_mbid:
            return []

        try:
            data = self.http.get_recording(
                recording_mbid,
                inc="artist-rels+work-rels+work-level-rels+recording-level-rels",
            )
            return data.get("relations", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch recording relationships for %s: %s", recording_mbid, exc)
            return []

    def get_composers_for_recording(self, recording_mbid: str) -> list[str]:
        """Extract composer/writer/lyricist names for a recording.

        Mirrors the legacy ``MusicBrainzClient.get_composers_for_track``
        parsing: composer/writer/lyricist credits attached directly to the
        recording, plus those attached to any linked Work entity (via
        ``work-level-rels``). Returns deduplicated names.
        """
        if not self.enabled or not recording_mbid:
            return []
        composers: list[str] = []
        for rel in self.get_recording_relationships(recording_mbid):
            rel_type = str(rel.get("type") or "").lower()
            if rel_type in ("composer", "writer", "lyricist"):
                target = rel.get("artist") or {}
                if target and target.get("name"):
                    composers.append(target["name"])
            work = rel.get("work") or {}
            for work_rel in work.get("relations") or []:
                work_rel_type = str(work_rel.get("type") or "").lower()
                if work_rel_type in ("composer", "writer", "lyricist"):
                    work_target = work_rel.get("artist") or {}
                    if work_target and work_target.get("name"):
                        composers.append(work_target["name"])
        return list(dict.fromkeys(composers))

    # ------------------------------------------------------------------
    # Genre-enriched lookups
    # ------------------------------------------------------------------

    def get_recording_genres(self, title: str, artist: str) -> list[str]:
        """Fetch genres for a recording via MusicBrainz.

        Uses ``inc=genres`` to get MusicBrainz genre tags directly.

        Args:
            title: Recording title.
            artist: Artist name.

        Returns:
            List of genre name strings.
        """
        from api_clients.musicbrainz_http import escape_lucene_special_chars

        if not self.enabled or not title or not artist:
            return []

        query = f'recording:"{escape_lucene_special_chars(title)}" AND artist:"{escape_lucene_special_chars(artist)}"'

        try:
            recordings = self.http.search_recordings_with_genres(query, limit=3)
            if not recordings:
                return []
            genres = []
            for rec in recordings:
                for g in (rec.get("genres") or []):
                    name = g.get("name") if isinstance(g, dict) else str(g)
                    if name and name not in genres:
                        genres.append(name)
            return genres
        except Exception as exc:
            logger.debug("Failed to fetch genres for '%s - %s': %s", artist, title, exc)
            return []


_service = None

_shared_mb_service: "MusicBrainzService | None" = None


def _get_service():
    global _service
    if _service is None:
        _service = MusicBrainzService()
    return _service


def get_shared_mb_service() -> "MusicBrainzService":
    """Process-wide MusicBrainzService sharing the shared HTTP client.

    Per-track code used to construct a fresh ``MusicBrainzService`` per track,
    re-reading the on-disk mbid cache on EVERY construction — one shared
    instance keeps the in-memory suggestion cache warm across the whole scan
    (and across scan threads; the disk cache IO is lock-protected).
    """
    global _shared_mb_service
    if _shared_mb_service is None:
        _shared_mb_service = MusicBrainzService(http_client=get_shared_mb_client())
    return _shared_mb_service


def lookup_recording_metadata(title: str, artist: str):
    return _get_service().lookup_recording_metadata(title, artist)


def merge_metadata(base: dict, mb: dict, overrides: dict | None = None):
    return _get_service().merge_metadata(base, mb, overrides)

def fetch_musicbrainz_release_metadata(release_id: str) -> Dict[str, Any] | None:
    """
    Fetch full release metadata including tracks + cover art.
    """

    try:
        # Shared client applies the canonical User-Agent, the 1 req/s throttle
        # and transport-layer retry/backoff — never a bare unthrottled call.
        data = get_shared_mb_client().get_release(
            release_id,
            inc="recordings+artist-credits+release-groups",
        )

        if not data:
            logger.debug("[MB] Release %s not found", release_id)
            return None

        rg = data.get("release-group", {})

        release_year = (
            (rg.get("first-release-date") or "")[:4]
            or (data.get("date") or "")[:4]
        )

        release_info = {
            "release_title": data.get("title"),
            "release_year": release_year,
            "artist": "",
            "disc_count": len(data.get("media", [])),
            "tracks": [],
            "release_mbid": data.get("id"),
        }

        # ✅ album artist — the PRIMARY credit only.  A collab release credits
        # multiple artists ("Weezer & Rivers Cuomo"); the full joined string
        # must never become ALBUMARTIST (Navidrome splits the album into two
        # artists).  The joined credit stays on the per-track artist.
        if data.get("artist-credit"):
            release_info["artist"] = primary_album_artist(data["artist-credit"])
            release_info["artist_credit"] = build_artist_credit_string(data["artist-credit"])

        # ✅ extract tracks
        for disc_index, media in enumerate(data.get("media", []), start=1):
            for track in media.get("tracks", []):
                recording = track.get("recording", {})

                release_info["tracks"].append({
                    "disc_number": disc_index,
                    "track_number": track.get("position"),
                    "title": track.get("title") or recording.get("title"),
                    "recording_mbid": recording.get("id"),
                    "duration": track.get("length"),
                    # Per-track artist: the recording's OWN artist-credit
                    # (full multi-artist string for collab tracks).  Falls
                    # back to the release's joined credit.
                    "artist": (
                        build_artist_credit_string(recording.get("artist-credit"))
                        if recording.get("artist-credit")
                        else release_info.get("artist_credit") or release_info.get("artist")
                    ),
                })

        # ✅ cover art (best effort) — CAA also requires a UA header
        try:
            from api_clients.coverartarchive import get_release_front_image_bytes
            cover = get_release_front_image_bytes(release_id)
            if cover:
                release_info["cover_art"] = cover
        except Exception:
            pass

        return release_info

    except Exception as e:
        logger.error("[MB RELEASE metadata] %s", e, exc_info=True)
        return None


def resolve_release_id(release_id: str) -> str:
    """Return a concrete release MBID for a release or release-group MBID.

    The download/search UI often hands over a *release-group* MBID (the search
    endpoint returns ``release-group`` results).  ``/ws/2/release/{id}`` 404s
    for a release-group id, which previously made ``start_release_download``
    fail and collapse the whole album into a single "album as one track" queue
    row.  Legacy parity: browse the release-group to its first release and
    return that concrete release MBID so per-track rows can be created.

    Returns the input unchanged when it is already a valid release lookup or
    when no release can be resolved.
    """
    if not release_id:
        return release_id

    try:
        http = _get_service().http
        data = http.get_release(release_id, inc="")
        if data and data.get("id"):
            return release_id
    except Exception:
        pass

    try:
        releases = http.browse_releases_for_group(release_id, inc="media", limit=50)
        if releases:
            # Smart selector: prefer OFFICIAL releases, then the one with the
            # MOST tracks (summed across all media).  The default browse order
            # frequently surfaces 4-track promos / sampler editions before the
            # canonical full album (e.g. a 15-track Greatest Hits).
            def _total_tracks(rel: dict) -> int:
                return sum(
                    int((m.get("track-count") or 0))
                    for m in (rel.get("media") or [])
                )

            official = [
                r for r in releases
                if str(r.get("status") or "").strip().lower() == "official"
            ]
            candidates = [r for r in (official or releases) if _total_tracks(r) > 0]
            candidates = candidates or (official or releases)
            best = max(candidates, key=_total_tracks)
            resolved = best["id"]
            logger.info(
                "[MB_RESOLVE] Release-group %s resolved to release %s "
                "(%s tracks, status %s)",
                release_id, resolved, _total_tracks(best), best.get("status"),
            )
            return resolved
    except Exception as exc:
        logger.warning("[MB_RESOLVE] Failed to resolve release-group %s: %s", release_id, exc)

    return release_id


def fetch_release_metadata(release_id: str):
    try:
        service = _get_service()
        http = service.http

        data = http.get_release(
            release_id,
            inc="recordings+artist-credits+release-groups"
        )

        if not data:
            return None

        rg = data.get("release-group", {})

        release_year = (
            (rg.get("first-release-date") or "")[:4]
            or (data.get("date") or "")[:4]
        )

        release_info = {
            "release_title": data.get("title"),
            "release_year": release_year,
            "artist": "",
            "disc_count": len(data.get("media", [])),
            "tracks": [],
            "release_mbid": data.get("id"),
        }

        if data.get("artist-credit"):
            # Primary credit only for the album artist — the joined
            # multi-artist string ("Weezer & Rivers Cuomo") would make
            # Navidrome split the album.  The full joined credit is kept on
            # ``artist_credit`` so per-track artists can carry it.
            release_info["artist"] = primary_album_artist(data["artist-credit"])
            release_info["artist_credit"] = build_artist_credit_string(data["artist-credit"])

        for disc_index, media in enumerate(data.get("media", []), start=1):
            for track in media.get("tracks", []):
                recording = track.get("recording", {})

                release_info["tracks"].append({
                    "disc_number": disc_index,
                    "track_number": track.get("position"),
                    "title": track.get("title") or recording.get("title"),
                    "recording_mbid": recording.get("id"),
                    "duration": track.get("length"),
                    # Per-track artist: the recording's OWN artist-credit
                    # (full multi-artist string when the recording credits
                    # several artists).  Falls back to the release's joined
                    # credit so collabs stay attributed on the track.
                    "artist": (
                        build_artist_credit_string(recording.get("artist-credit"))
                        if recording.get("artist-credit")
                        else release_info.get("artist_credit") or release_info.get("artist")
                    ),
                })

        return release_info

    except Exception as e:
        logger.error("[MB RELEASE track fetch] %s", e, exc_info=True)
        return None


def _mb_artist_credit_name(artist_credit) -> str:
    """Return the primary artist name from a MusicBrainz artist-credit."""
    if isinstance(artist_credit, list) and artist_credit:
        first = artist_credit[0]
        if isinstance(first, dict):
            return str(first.get("name") or "")
    elif isinstance(artist_credit, str):
        return artist_credit
    return ""


def _cover_art_url(rg_id: str, release_id: str = "") -> str:
    """CoverArtArchive URL for a release-group (falls back to release art).

    Mirrors the legacy album lookup: release-group art is preferred because it
    is more reliably available than art for a specific pressing/reissue MBID.
    """
    if rg_id:
        return f"https://coverartarchive.org/release-group/{rg_id}/front-250"
    if release_id:
        return f"https://coverartarchive.org/release/{release_id}/front-250"
    return ""


def _lookup_existing_mbid(existing_mbid: str, artist: str, album: str) -> dict | None:
    """Direct lookup of the stored MBID — release first, then release-group.

    Returns a result dict shaped for the album-page JS (``mbid``, ``artist``,
    ``primary_type``, ``secondary_types``, ``first_release_date``,
    ``cover_art_url``, ``confidence``, ``is_stored_mbid``, ``mbid_type``) or
    ``None`` when neither resolves.  This is the legacy behaviour: the stored
    release is surfaced so the user can compare it against text-search hits.
    """
    if not existing_mbid:
        return None
    client = get_shared_mb_client()

    # Try as a release MBID first (musicbrainz_album_mbid stores a release).
    try:
        rel_data = client.get_release(existing_mbid, inc="artist-credits+release-groups")
        if rel_data:
            # Primary credit only — the joined multi-artist string would split
            # the album on Navidrome if applied as album artist.
            rel_artist = primary_album_artist(rel_data.get("artist-credit") or []) or artist
            rg = rel_data.get("release-group") or {}
            rg_id = rg.get("id", "")
            primary_type = rg.get("primary-type", "Album")
            secondary_types = _parse_secondary_types(rg.get("secondary-types"))
            display_date = rg.get("first-release-date", "") or rel_data.get("date", "")
            return {
                "mbid": existing_mbid,
                "title": rel_data.get("title", album),
                "artist": rel_artist,
                "primary_type": primary_type,
                "secondary_types": secondary_types,
                "first_release_date": display_date,
                "cover_art_url": _cover_art_url(rg_id, existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release",
            }
    except Exception as exc:
        logger.debug("[MB] Stored release lookup failed for %s: %s", existing_mbid, exc)

    # Fall back to a release-group MBID.
    try:
        rg_data = client.get_release_group(existing_mbid, inc="artist-credits")
        if rg_data:
            rg_artist = _mb_artist_credit_name(rg_data.get("artist-credit") or []) or artist
            return {
                "mbid": existing_mbid,
                "title": rg_data.get("title", album),
                "artist": rg_artist,
                "primary_type": rg_data.get("primary-type", "Album"),
                "secondary_types": _parse_secondary_types(rg_data.get("secondary-types")),
                "first_release_date": rg_data.get("first-release-date", ""),
                "cover_art_url": _cover_art_url(existing_mbid),
                "confidence": 1.0,
                "source": "musicbrainz",
                "is_stored_mbid": True,
                "mbid_type": "release-group",
            }
    except Exception as exc:
        logger.debug("[MB] Stored release-group lookup failed for %s: %s", existing_mbid, exc)
    return None


def lookup_musicbrainz_album(artist: str, album: str, existing_mbid: str = "") -> dict:
    """Look up an album on MusicBrainz and return release candidates.

    Legacy-compatible contract (the album-page JS consumes it):
    - The stored MBID (when present) is resolved directly — release first,
      then release-group — and surfaced first as ``is_stored_mbid``.
    - A text search for release groups follows, scored with a title/artist
      similarity confidence.

    Returns ``{"results": [...]}`` so the album page's
    ``displayAlbumResults(data.results, 'musicbrainz')`` renders matches.
    """
    results: list[dict[str, Any]] = []

    # 1. Direct lookup of the currently stored MBID (release → release-group).
    if existing_mbid:
        stored = _lookup_existing_mbid(existing_mbid, artist, album)
        if stored:
            results.append(stored)
            logger.info("[MB_LOOKUP] Found stored %s MBID %s: %s by %s",
                        stored["mbid_type"], existing_mbid, stored["title"], stored["artist"])

    # 2. Text search for release groups (mirrors the legacy query).
    query = f'release:"{escape_lucene_special_chars(album)}" AND artist:"{escape_lucene_special_chars(artist)}"'
    try:
        groups = get_shared_mb_client().search_release_groups(query, limit=10)
    except Exception as exc:
        logger.warning("[MB_LOOKUP] MusicBrainz album search unavailable: %s", exc)
        groups = []

    seen_mbids = {r["mbid"] for r in results}
    for rg in groups or []:
        rg_id = rg.get("id", "")
        if not rg_id or rg_id in seen_mbids:
            continue
        rg_title = rg.get("title", "")
        primary_type = rg.get("primary-type", "Album")
        secondary_types = _parse_secondary_types(rg.get("secondary-types"))
        first_release = rg.get("first-release-date", "")
        rg_artist = _mb_artist_credit_name(rg.get("artist-credit") or [])
        confidence = calculate_match_score(rg_title, rg.get("artist-credit") or [], album, artist)
        results.append({
            "mbid": rg_id,
            "title": rg_title,
            "artist": rg_artist,
            "primary_type": primary_type,
            "secondary_types": secondary_types,
            "first_release_date": first_release,
            "cover_art_url": _cover_art_url(rg_id),
            "confidence": round(confidence, 3),
            "source": "musicbrainz",
            "is_stored_mbid": False,
            "mbid_type": "release-group",
        })
        seen_mbids.add(rg_id)

    # Stored MBID first, then by confidence desc.
    stored = [r for r in results if r.get("is_stored_mbid")]
    others = sorted(
        [r for r in results if not r.get("is_stored_mbid")],
        key=lambda r: r.get("confidence") or 0.0,
        reverse=True,
    )
    return {"results": (stored + others)[:11]}


def get_release_group_releases(rg_mbid: str, include_track_counts: bool = False) -> dict:
    """Fetch all releases within a release group.

    Args:
        rg_mbid: MusicBrainz release-group MBID.
        include_track_counts: If True, also fetches track counts for each
            release via the releases browse endpoint (one extra API call).

    Returns:
        Dict with ``success`` and ``releases`` list.  Each release dict is
        NORMALISED to the album-page release-picker contract (same shape as
        ``get_musicbrainz_best_release``): ``id``, ``title``, ``date``,
        ``country``, ``status``, ``disambiguation``, ``track_count``,
        ``disc_count``, ``formats`` (list) and ``cover_art_url``.  The raw
        MusicBrainz dicts carry ``media`` arrays, NOT a flat ``formats``
        list — the picker's ``r.formats.join(' + ')`` throws on the raw
        shape, which is exactly the "modal opens but no releases load"
        regression.
    """
    try:
        data = get_shared_mb_client().get_release_group(rg_mbid, inc="releases")
        if not data:
            return {"success": False, "error": "No release-group data returned"}
        raw_releases = data.get("releases", []) or []

        releases: list[dict[str, Any]] = []
        for r in raw_releases:
            media = r.get("media") or []
            total_tracks = sum(int(m.get("track-count", 0) or 0) for m in media)
            formats = list({
                str(m.get("format") or "").strip()
                for m in media if m.get("format")
            })
            releases.append({
                "id": r.get("id", ""),
                "title": r.get("title", ""),
                "date": r.get("date", ""),
                "country": r.get("country", ""),
                "status": r.get("status", ""),
                "disambiguation": r.get("disambiguation", ""),
                "track_count": total_tracks,
                "disc_count": len(media),
                "formats": [f for f in formats if f],
                "cover_art_url": (
                    f"https://coverartarchive.org/release/{r.get('id')}/front-250"
                    if r.get("id") else ""
                ),
            })

        if include_track_counts and releases:
            _enrich_releases_with_track_counts(releases, rg_mbid=rg_mbid)

        return {"success": True, "releases": releases}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _enrich_releases_with_track_counts(releases: list[dict], rg_mbid: str | None = None) -> None:
    """Mutate releases in-place, adding ``track_count`` from MusicBrainz.

    Uses the browse endpoint to fetch all releases for the release-group with
    media info, then maps back by release MBID.  ``rg_mbid`` is passed
    directly (the flattened releases from ``get_release_group_releases`` do
    NOT carry the nested ``release-group`` object, so deriving it from
    ``releases[0]`` always failed and no counts were ever attached).
    """
    if not releases:
        return
    # Fall back to deriving the group from the first release's nested
    # release-group (the raw MB shape / best-release path).
    if not rg_mbid:
        first_rg = releases[0].get("release-group")
        if isinstance(first_rg, dict):
            rg_mbid = first_rg.get("id")
    if not rg_mbid:
        return

    try:
        browse_releases = get_shared_mb_client().browse_releases_for_group(
            rg_mbid, inc="media", limit=100,
        )
        # Build a lookup: release MBID → total track count
        tc_lookup: dict[str, int] = {}
        for rel in browse_releases:
            rel_id = rel.get("id")
            if not rel_id:
                continue
            total = 0
            for medium in rel.get("media", []):
                total += int(medium.get("track-count", 0) or 0)
            if total > 0:
                tc_lookup[rel_id] = total

        for rel in releases:
            rel_id = rel.get("id")
            if rel_id and rel_id in tc_lookup:
                rel["track_count"] = tc_lookup[rel_id]
    except Exception as exc:
        logger.debug("Failed to fetch track counts for release-group %s: %s", rg_mbid, exc)


def compare_musicbrainz_release(artist: str, album: str, rg_mbid: str) -> dict:
    """Compare a MusicBrainz release's tracklist with the local library.

    Ported from the old_system album-compare engine (per-track matching +
    field-diff recommendations).  For every MusicBrainz track it tries to
    find the matching library track (by track number with a title-similarity
    gate, exact normalised title, fuzzy title ≥ 80 %, then a core-title /
    parenthetical-stripped pass, and finally a cross-disc fuzzy pass) and
    reports which fields differ (title, track_number, year, mbid, duration,
    disc_number) so the album page can recommend and apply corrections.

    Matches as many tracks as it can; anything it cannot match is returned as
    unmatched (``matched: False``) and library tracks not claimed by any MB
    track are returned in ``extra_tracks``.

    Returns the frontend contract expected by ``album_detail.js``:
    ``{success, mb_title, mb_year, mb_artist, release_group_mbid,
    comparison, extra_tracks, tracks_needing_update, total_tracks}``.
    """
    try:
        from db.engine import db_session
        from sqlalchemy import text
        import difflib as _difflib

        # Resolve a concrete RELEASE MBID for the tracklist fetch.  The input
        # (``rg_mbid``) can be either a release-GROUP MBID (the shared search
        # modal hands back the group) or a concrete RELEASE MBID (older lookup
        # flows handed back a release).  A release id is used directly —
        # browsing it as if it were a group 404s and wastes a slow round-trip;
        # group ids go through the best-release resolver so the right
        # edition's tracklist is compared.
        _direct = None
        try:
            _direct = get_shared_mb_client().get_release(rg_mbid, inc="")
        except Exception:
            _direct = None

        if _direct and _direct.get("id"):
            release_id = rg_mbid
        else:
            best = get_musicbrainz_best_release(artist, album, rg_mbid)
            best_release = (best or {}).get("best_release")
            release_id = (best_release or {}).get("id") or rg_mbid
            # ``fetch_musicbrainz_release_metadata`` needs a concrete RELEASE
            # MBID (a release-group 404s on /ws/2/release/{id}) — resolve the
            # group to a real release when best-release resolution came up
            # empty.
            if not (best_release or {}).get("id"):
                resolved = resolve_release_id(rg_mbid)
                if resolved and resolved != rg_mbid:
                    release_id = resolved

        mb_release = fetch_musicbrainz_release_metadata(release_id)
        if not mb_release:
            return {"success": False, "error": "Could not fetch MusicBrainz release data"}

        mb_tracks = mb_release.get("tracks", [])
        mb_year = str(mb_release.get("release_year") or "")
        mb_release_title = str(mb_release.get("release_title") or "")

        # Load library tracks for this album.  Case-insensitive matching —
        # consistent with the album detail page route — so a URL casing
        # difference never empties the comparison.
        library_tracks: list[dict[str, Any]] = []
        try:
            with db_session() as session:
                result = session.execute(
                    text(_COMPARE_LIBRARY_TRACKS_SQL),
                    {"artist": artist, "album": album},
                )
                library_tracks = [dict(r._mapping) for r in result.fetchall()]
        except Exception as exc:
            logger.debug("[MB_COMPARE] library track fetch failed: %s", exc)

        # ``disc_number`` / ``track_number`` are TEXT columns in PostgreSQL.
        # The old ``COALESCE(disc_number, 1)`` integer literals made the whole
        # query fail on Postgres ("COALESCE types text and integer cannot be
        # matched") — the exception was swallowed above and surfaced as the
        # misleading "No library tracks found for this album".  String
        # literals keep the query portable (SQLite tests + Postgres); the
        # Python sort below restores numeric disc/track ordering.
        def _disc_track_key(t: dict[str, Any]) -> tuple[int, int]:
            def _num(v: Any, default: int) -> int:
                s = str(v or "").strip()
                if not s:
                    return default
                try:
                    return int(s.split("/")[0].strip())
                except (TypeError, ValueError):
                    return default
            return (_num(t.get("disc_number"), 1), _num(t.get("track_number"), 999))

        library_tracks.sort(key=_disc_track_key)

        if not library_tracks:
            return {
                "success": False,
                "error": "No library tracks found for this album",
                "comparison": [],
            }

        # Normalisation helper mirroring the old_system passes.
        def _norm(value: str) -> str:
            return re.sub(r"\s+", " ", str(value or "").lower().strip())

        def _core(value: str) -> str:
            """Title truncated at the first ( or [ — strips venue/year/version
            annotations that the local library usually omits."""
            return re.sub(r"\s*[\(\[].+$", "", _norm(value)).strip()

        # Lookups: (disc, track_number) -> track, (disc, norm_title) -> track.
        lib_by_tracknum: dict[tuple, dict] = {}
        lib_by_title: dict[tuple, dict] = {}
        for t in library_tracks:
            disc = int(t.get("disc_number") or 1)
            tn = t.get("track_number")
            if tn is not None:
                try:
                    lib_by_tracknum[(disc, int(str(tn).split("/")[0].strip()))] = t
                except (TypeError, ValueError):
                    pass
            lib_by_title[(disc, _norm(t.get("title") or ""))] = t

        matched_lib_ids: set = set()
        comparison: list[dict[str, Any]] = []

        # Duration tolerance (seconds) — legacy parity.
        _DURATION_TOLERANCE_SEC = 5.0
        # Minimum title similarity to trust a track-number match.
        _TRACK_NUM_TITLE_SIM_MIN = 0.30

        for mb_track in mb_tracks:
            disc = int(mb_track.get("disc_number") or 1)
            mb_num_raw = mb_track.get("track_number")
            try:
                mb_num = int(str(mb_num_raw).split("/")[0].strip()) if mb_num_raw is not None else None
            except (TypeError, ValueError):
                mb_num = None
            mb_title = str(mb_track.get("title") or "")
            norm_mb = _norm(mb_title)
            norm_mb_core = _core(mb_title)
            mb_recording_id = str(mb_track.get("recording_mbid") or "")
            mb_duration_ms = mb_track.get("duration")
            mb_duration_sec = (int(mb_duration_ms) / 1000.0) if mb_duration_ms else None

            lib_track = None

            # 1. Match by track number — only when titles are reasonably similar.
            if mb_num is not None and not lib_track:
                candidate = lib_by_tracknum.get((disc, mb_num))
                if candidate is not None:
                    if not norm_mb or _difflib.SequenceMatcher(
                        None, norm_mb, _norm(candidate.get("title") or "")
                    ).ratio() >= _TRACK_NUM_TITLE_SIM_MIN:
                        lib_track = candidate

            # 2. Match by exact normalised title (same disc).
            if lib_track is None:
                lib_track = lib_by_title.get((disc, norm_mb))

            # 3. Fuzzy title match (≥ 80%) — same disc, unclaimed tracks only.
            if lib_track is None:
                best_ratio, best_t = 0.0, None
                for t in library_tracks:
                    if int(t.get("disc_number") or 1) != disc:
                        continue
                    if t.get("id") in matched_lib_ids:
                        continue
                    ratio = _difflib.SequenceMatcher(None, norm_mb, _norm(t.get("title") or "")).ratio()
                    if ratio > best_ratio and ratio >= 0.80:
                        best_ratio, best_t = ratio, t
                lib_track = best_t

            # 4. Core-title match: strip parenthetical/bracketed suffixes and retry.
            if lib_track is None and norm_mb_core and norm_mb_core != norm_mb:
                candidate = lib_by_title.get((disc, norm_mb_core))
                if candidate is not None and candidate.get("id") not in matched_lib_ids:
                    lib_track = candidate
                if lib_track is None:
                    best_ratio, best_t = 0.0, None
                    for t in library_tracks:
                        if int(t.get("disc_number") or 1) != disc:
                            continue
                        if t.get("id") in matched_lib_ids:
                            continue
                        ratio = _difflib.SequenceMatcher(None, norm_mb_core, _norm(t.get("title") or "")).ratio()
                        if ratio > best_ratio and ratio >= 0.80:
                            best_ratio, best_t = ratio, t
                    lib_track = best_t

            entry: dict[str, Any] = {
                "mb_track_number": mb_num,
                "mb_disc_number": disc,
                "mb_title": mb_title,
                "mb_artist": "",
                "mb_recording_id": mb_recording_id,
                "mb_year": mb_year,
                "mb_duration": None,
                "mb_duration_sec": int(mb_duration_sec) if mb_duration_sec else None,
                "library_track_id": None,
                "library_title": None,
                "library_track_number": None,
                "library_disc_number": None,
                "library_artist": None,
                "library_year": None,
                "library_duration": None,
                "matched": False,
                "needs_update": False,
                "diff_fields": [],
            }

            if lib_track is not None:
                matched_lib_ids.add(lib_track["id"])
                entry.update({
                    "matched": True,
                    "library_track_id": lib_track.get("id"),
                    "library_title": lib_track.get("title", ""),
                    "library_track_number": lib_track.get("track_number"),
                    "library_disc_number": int(lib_track.get("disc_number") or 1),
                    "library_artist": lib_track.get("artist", ""),
                    "library_year": str(lib_track.get("year") or ""),
                })
                raw_lib_dur = lib_track.get("duration")
                lib_duration_sec = None
                if raw_lib_dur not in (None, "", 0, "0"):
                    try:
                        val = float(raw_lib_dur)
                        lib_duration_sec = (val / 1000.0) if val > 10000 else val
                        lib_duration_sec = lib_duration_sec if lib_duration_sec > 0 else None
                    except (TypeError, ValueError):
                        lib_duration_sec = None
                entry["library_duration"] = lib_duration_sec

                def _fmt_dur(sec):
                    if sec is None:
                        return None
                    s = int(round(sec))
                    return f"{s // 60}:{s % 60:02d}"

                entry["mb_duration"] = _fmt_dur(mb_duration_sec)
                entry["library_duration"] = _fmt_dur(lib_duration_sec)

                diff_fields: list[str] = []
                # Title differs (ignore an expected "(Artist Cover)" suffix).
                lib_title = str(lib_track.get("title") or "")
                if mb_title and mb_title != lib_title:
                    cover_match = re.search(r'\([^)]*\bcover\b[^)]*\)', lib_title, re.IGNORECASE)
                    if cover_match:
                        stripped = re.sub(r'\s*\([^)]*\bcover\b[^)]*\)', '', lib_title, flags=re.IGNORECASE).strip()
                        if stripped.lower() != mb_title.lower():
                            diff_fields.append("title")
                    else:
                        diff_fields.append("title")
                # Track number differs.
                lib_tn = lib_track.get("track_number")
                if mb_num is not None and str(mb_num) != str(lib_tn or ""):
                    diff_fields.append("track_number")
                # Year differs.
                lib_year = str(lib_track.get("year") or "")
                if mb_year and mb_year != lib_year:
                    diff_fields.append("year")
                # Missing recording MBID.
                lib_mbid = str(lib_track.get("mbid") or "").strip()
                if mb_recording_id and not lib_mbid:
                    diff_fields.append("mbid")
                # Duration differs by more than the tolerance.
                if mb_duration_sec is not None and lib_duration_sec is not None:
                    if abs(mb_duration_sec - lib_duration_sec) > _DURATION_TOLERANCE_SEC:
                        diff_fields.append("duration")
                # Disc number differs.
                if int(lib_track.get("disc_number") or 1) != disc:
                    diff_fields.append("disc_number")

                # Remove fields the user has permanently ignored.
                import json as _json_cmp
                try:
                    ignored = set(_json_cmp.loads(lib_track.get("mb_ignored_fields") or "[]"))
                except Exception:
                    ignored = set()
                diff_fields = [f for f in diff_fields if f not in ignored]
                entry["diff_fields"] = diff_fields
                entry["needs_update"] = len(diff_fields) > 0

            comparison.append(entry)

        # ── Cross-disc matching pass ────────────────────────────────────────
        # Some MB tracks remain unmatched because the library stores them under
        # a different disc number.  Match those against unclaimed library tracks
        # on ANY disc (fuzzy ≥ 80%) and promote them with disc_number flagged.
        matched_mb_recording_ids = {e.get("mb_recording_id", "") for e in comparison if e.get("matched")}
        for mb_track in mb_tracks:
            mb_recording_id = str(mb_track.get("recording_mbid") or "")
            if mb_recording_id and mb_recording_id in matched_mb_recording_ids:
                continue
            disc = int(mb_track.get("disc_number") or 1)
            mb_title = str(mb_track.get("title") or "")
            norm_mb = _norm(mb_title)
            norm_mb_core = _core(mb_title)
            mb_num_raw = mb_track.get("track_number")
            try:
                mb_num = int(str(mb_num_raw).split("/")[0].strip()) if mb_num_raw is not None else None
            except (TypeError, ValueError):
                mb_num = None

            already_matched = any(
                e.get("matched") and e.get("mb_title") == mb_title
                and int(e.get("mb_disc_number") or 1) == disc
                and e.get("mb_track_number") == mb_num
                for e in comparison
            )
            if already_matched:
                continue

            best_ratio, best_lib = 0.0, None
            for t in library_tracks:
                if t.get("id") in matched_lib_ids:
                    continue
                lib_disc = int(t.get("disc_number") or 1)
                if lib_disc == disc:
                    continue  # same disc already handled in the main pass
                ratio = _difflib.SequenceMatcher(None, norm_mb, _norm(t.get("title") or "")).ratio()
                if ratio < 0.80 and norm_mb_core and norm_mb_core != norm_mb:
                    ratio = max(ratio, _difflib.SequenceMatcher(None, norm_mb_core, _norm(t.get("title") or "")).ratio())
                if ratio > best_ratio and ratio >= 0.80:
                    best_ratio, best_lib = ratio, t

            if best_lib is None:
                continue

            matched_lib_ids.add(best_lib["id"])
            if mb_recording_id:
                matched_mb_recording_ids.add(mb_recording_id)

            mb_duration_ms = mb_track.get("duration")
            mb_duration_sec = (int(mb_duration_ms) / 1000.0) if mb_duration_ms else None

            entry = {
                "mb_track_number": mb_num,
                "mb_disc_number": disc,
                "mb_title": mb_title,
                "mb_artist": "",
                "mb_recording_id": mb_recording_id,
                "mb_year": mb_year,
                "mb_duration": None,
                "mb_duration_sec": int(mb_duration_sec) if mb_duration_sec else None,
                "library_track_id": best_lib["id"],
                "library_title": best_lib.get("title", ""),
                "library_track_number": best_lib.get("track_number"),
                "library_disc_number": int(best_lib.get("disc_number") or 1),
                "library_artist": best_lib.get("artist", ""),
                "library_year": str(best_lib.get("year") or ""),
                "library_duration": None,
                "matched": True,
                "cross_disc_match": True,
                "needs_update": False,
                "diff_fields": [],
            }

            diff_fields = []
            lib_title = str(best_lib.get("title") or "")
            if mb_title and mb_title != lib_title:
                cover_match = re.search(r'\([^)]*\bcover\b[^)]*\)', lib_title, re.IGNORECASE)
                if cover_match:
                    stripped = re.sub(r'\s*\([^)]*\bcover\b[^)]*\)', '', lib_title, flags=re.IGNORECASE).strip()
                    if stripped.lower() != mb_title.lower():
                        diff_fields.append("title")
                else:
                    diff_fields.append("title")
            if mb_num is not None and str(mb_num) != str(best_lib.get("track_number") or ""):
                diff_fields.append("track_number")
            lib_year = str(best_lib.get("year") or "")
            if mb_year and mb_year != lib_year:
                diff_fields.append("year")
            lib_mbid = str(best_lib.get("mbid") or "").strip()
            if mb_recording_id and not lib_mbid:
                diff_fields.append("mbid")
            # disc_number mismatch is the point of a cross-disc match.
            diff_fields.append("disc_number")

            import json as _json_xdisc
            try:
                ignored = set(_json_xdisc.loads(best_lib.get("mb_ignored_fields") or "[]"))
            except Exception:
                ignored = set()
            diff_fields = list(dict.fromkeys(f for f in diff_fields if f not in ignored))
            entry["diff_fields"] = diff_fields
            entry["needs_update"] = len(diff_fields) > 0
            comparison.append(entry)

        tracks_needing_update = sum(1 for c in comparison if c.get("needs_update"))

        # Library tracks never claimed by any MB track = "extra" tracks.
        extra_tracks = []
        for t in library_tracks:
            if t["id"] not in matched_lib_ids:
                extra_tracks.append({
                    "library_track_id": t["id"],
                    "library_title": t.get("title", ""),
                    "library_track_number": t.get("track_number"),
                    "library_disc_number": int(t.get("disc_number") or 1),
                    "library_artist": t.get("artist", ""),
                })

        return {
            "success": True,
            "mb_title": mb_release_title,
            "mb_year": mb_year,
            "mb_artist": str(mb_release.get("artist") or ""),
            "release_group_mbid": rg_mbid,
            "release_mbid": release_id,
            "comparison": comparison,
            "extra_tracks": extra_tracks,
            "tracks_needing_update": tracks_needing_update,
            "total_tracks": len(comparison),
        }
    except Exception as exc:
        logger.error("[MB_COMPARE] Error: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}


def _get_local_track_count(artist: str, album: str) -> int:
    """Query the local database for the number of tracks in an album."""
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            # Case-insensitive + NULL-safe matching, mirroring the album-detail
            # page route query — the URL-decoded artist/album names frequently
            # differ in case from the stored values, and an exact match would
            # report 0 tracks (confidence 0.5 → "not confident enough" on the
            # album page's auto-match).
            result = session.execute(
                text("SELECT COUNT(*) AS cnt FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) AND LOWER(COALESCE(album, '')) = LOWER(:album)"),
                {"artist": artist, "album": album},
            )
            row = result.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def get_musicbrainz_best_release(artist: str, album: str, rg_mbid: str) -> dict:
    """Find the best matching release inside a release group.

    Matches the legacy (old_system) response contract so the album-page JS
    (``autoPickRelease``) works: returns ``releases`` (full release list for
    the picker), ``best_release`` (highest-scoring release), ``confidence``
    (0.0–1.0) and ``local_track_count``.

    Scoring:
    - Track-count proximity to the local album (dominant).
    - Official/Promotional status bonus.
    - Release date (older editions like anniversary remasters score slightly
      lower than the original).
    - Release-title similarity to the local album name.

    Confidence is 1.0 when the best release's track count exactly matches the
    local album, otherwise scaled down by ``1.0 - (diff * 0.2)`` (legacy
    parity).  A confidence >= 0.8 lets the frontend apply the match directly.
    """
    try:
        # ``get_shared_mb_client`` is defined in this module (singleton client).
        client = get_shared_mb_client()

        # Browse all releases in the group WITH media so track counts,
        # disc counts and formats are available without extra calls.
        # NOTE: ``recordings`` is intentionally NOT requested — the release
        # BROWSE endpoint rejects some inc combinations and the release
        # detail (with recordings) is fetched lazily by the picker's
        # "Tracks" button instead.
        releases_raw = client.browse_releases_for_group(
            rg_mbid, inc="media+labels", limit=50,
        )
        releases: list[dict[str, Any]] = []
        for r in releases_raw or []:
            media = r.get("media") or []
            total_tracks = sum(int(m.get("track-count", 0) or 0) for m in media)
            disc_count = len(media)
            formats = list({str(m.get("format") or "").strip() for m in media if m.get("format")})
            releases.append({
                "id": r.get("id", ""),
                "title": r.get("title", ""),
                "date": r.get("date", ""),
                "country": r.get("country", ""),
                "status": r.get("status", ""),
                "disambiguation": r.get("disambiguation", ""),
                "track_count": total_tracks,
                "disc_count": disc_count,
                "formats": [f for f in formats if f],
                "cover_art_url": (
                    f"https://coverartarchive.org/release/{r.get('id')}/front-250"
                    if r.get("id") else ""
                ),
            })

        # Sort chronologically (blank dates last) so the picker is stable.
        releases.sort(key=lambda x: (x.get("date") == "", x.get("date") or ""))

        if not releases:
            return {
                "success": True,
                "releases": [],
                "best_release": None,
                "confidence": 0,
                "local_track_count": None,
            }

        local_tc = _get_local_track_count(artist, album)
        local_tc = local_tc if local_tc > 0 else None

        def _score_release(rel: dict) -> float:
            score = 0.0
            # Track-count proximity: dominant signal (legacy parity: -100 per
            # unit of difference).
            if local_tc is not None:
                diff = abs(local_tc - int(rel.get("track_count") or 0))
                score -= diff * 100.0
            # Official releases get a modest bonus.
            if (rel.get("status") or "").lower() == "official":
                score += 50.0
            # Older editions (anniversary remasters etc.) score slightly lower.
            date = (rel.get("date") or "").strip()
            if date and date[:4].isdigit():
                score += max(0.0, 2100.0 - int(date[:4])) * 0.01
            # Title similarity to the local album nudges the correct edition.
            if album and rel.get("title"):
                score += _similarity(album.lower(), str(rel["title"]).lower()) * 30.0
            return score

        scored = sorted(
            ((rel, _score_release(rel)) for rel in releases),
            key=lambda x: x[1],
            reverse=True,
        )
        best_release, best_score = scored[0][0], scored[0][1]

        # Confidence: 1.0 on exact track-count match, else scaled down.
        confidence = 0.0
        if local_tc is not None:
            if int(best_release.get("track_count") or 0) == local_tc:
                confidence = 1.0
            else:
                diff = abs(local_tc - int(best_release.get("track_count") or 0))
                confidence = max(0.0, 1.0 - (diff * 0.2))
        else:
            confidence = 0.5  # no local data → not confident

        return {
            "success": True,
            "releases": releases,
            "best_release": best_release,
            "confidence": round(confidence, 2),
            "local_track_count": local_tc,
        }
    except Exception as exc:
        logger.error("[MB_BEST_RELEASE] Error: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}

