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

import difflib
import json
import logging
import os
from typing import Any, List, Dict, Tuple
import httpx
import time

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
    edition_annotations_compatible,
)

logger = logging.getLogger(__name__)

CACHE_FILE = "/tmp/mbid_cache.json" if os.path.exists("/tmp") else "mbid_cache.json"

# Entries per batched MusicBrainz search (``lookup_album_metadata``).  Each
# entry contributes a Lucene OR-group to the query — beyond ~20 the URL grows
# unwieldy and the results get diluted across too many candidates.
_MB_BATCH_CHUNK = 20


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


def calculate_match_score(mb_title, mb_artist_credit, local_album, local_artist) -> float:
    title_sim = difflib.SequenceMatcher(None, normalize_string(local_album), normalize_string(mb_title)).ratio()

    artist_name = ""
    if isinstance(mb_artist_credit, list) and mb_artist_credit:
        if isinstance(mb_artist_credit[0], dict):
            artist_name = mb_artist_credit[0].get("name", "")
    elif isinstance(mb_artist_credit, str):
        artist_name = mb_artist_credit

    artist_sim = difflib.SequenceMatcher(None, normalize_string(local_artist), normalize_string(artist_name)).ratio()

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
        query_title = normalize_title_for_lucene_query(title)
        query = f'recording:"{escape_lucene_special_chars(query_title)}" AND artist:"{escape_lucene_special_chars(artist)}"'

        try:
            recordings = self.http.search_recordings(query, limit=limit)

            best_mbid = ""
            best_score = 0.0
            norm_title = normalize_title_for_mbid_match(title)

            for rec in recordings:
                sim = difflib.SequenceMatcher(
                    None, norm_title, normalize_title_for_lookup(rec.get("title") or "")
                ).ratio()

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

def _first_isrc(recording: dict) -> str | None:
    """First ISRC from a MusicBrainz recording or search document.

    Both the JSON search docs and the recording entity expose ISRCs as an
    ``isrcs`` array; a defensive ``isrc-list`` alias covers older payloads.
    """
    isrcs = recording.get("isrcs") or recording.get("isrc-list") or []
    if isinstance(isrcs, list):
        for raw in isrcs:
            value = str(raw or "").strip()
            if value:
                return value
    value = str(recording.get("isrc") or "").strip()
    return value or None


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
    ) -> Dict[str, Dict[str, Any]]:
        """Batch MusicBrainz metadata for many tracks in ONE search per chunk.

        Builds a single Lucene OR query over ``(recording:"<title>" AND
        artist:"<artist>")`` groups (chunked to keep URL length sane) and
        scores each hit with the same bracket-preserving similarity used by
        ``get_suggested_mbid``.  Metadata — album, year, artist credit,
        recording MBID — comes straight from the search documents, replacing
        the per-track (search + recording lookup) request pair with a single
        batched search.

        Returns ``{artist.lower()::title.lower(): metadata}`` for matched
        entries only; unmatched entries fall back to the per-track lookup.
        Resolved MBIDs are written to the persistent mbid cache so later
        scans skip the search entirely.
        """
        if not self.enabled:
            return {}
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
                    for rec in recordings:
                        sim = difflib.SequenceMatcher(
                            None, norm_title, normalize_title_for_lookup(rec.get("title") or "")
                        ).ratio()
                        if sim > best_score:
                            best_score = sim
                            best = rec
                    mbid = (best or {}).get("id", "")
                    if not best or not mbid:
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
        query_title = normalize_title_for_lucene_query(title)
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
            sim = difflib.SequenceMatcher(
                None,
                norm_title,
                normalize_title_for_lookup(group.get("title") or ""),
            ).ratio()
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
        query_title = normalize_title_for_lucene_query(title)
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
        return difflib.SequenceMatcher(None, norm_rg, norm_title).ratio() >= 0.85

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

        query = f'recording:"{escape_lucene_special_chars(title)}" AND artist:"{escape_lucene_special_chars(artist)}"'

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

        query = f'artist:"{escape_lucene_special_chars(artist_name)}" AND releasegroup:"{escape_lucene_special_chars(album_name)}"'

        try:
            groups = self.http.search_release_groups(query, limit=limit)
        except Exception:
            return []

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


def _get_service():
    global _service
    if _service is None:
        _service = MusicBrainzService()
    return _service


def lookup_recording_metadata(title: str, artist: str):
    return _get_service().lookup_recording_metadata(title, artist)


def merge_metadata(base: dict, mb: dict, overrides: dict | None = None):
    return _get_service().merge_metadata(base, mb, overrides)

def fetch_musicbrainz_release_metadata(release_id: str) -> Dict[str, Any] | None:
    """
    Fetch full release metadata including tracks + cover art.
    """

    try:
        headers = {
            "User-Agent": "musicbrainz-enrichment-service",
            "Accept": "application/json"
        }

        url = f"https://musicbrainz.org/ws/2/release/{release_id}"
        params = {
            "inc": "recordings+artist-credits+release-groups",
            "fmt": "json"
        }

        # ✅ simple rate limit
        time.sleep(1.0)

        response = httpx.get(url, headers=headers, params=params, timeout=10)

        if response.status_code == 404:
            logger.debug(f"[MB] Release {release_id} not found")
            return None

        response.raise_for_status()
        data = response.json()

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

        # ✅ album artist
        if data.get("artist-credit"):
            release_info["artist"] = build_artist_credit_string(data["artist-credit"])

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
                })

        # ✅ cover art (best effort)
        try:
            cover_url = f"https://coverartarchive.org/release/{release_id}/front-500"
            cover = httpx.get(cover_url, timeout=5)
            if cover.status_code == 200:
                release_info["cover_art"] = cover.content
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
        releases = http.get(
            "release/",
            params={"fmt": "json", "release-group": release_id, "limit": 1},
            timeout=10.0,
        )
        releases = releases.get("releases") if isinstance(releases, dict) else []
        if releases and releases[0].get("id"):
            resolved = releases[0]["id"]
            logger.info(
                "[MB_RESOLVE] Release-group %s resolved to release %s",
                release_id,
                resolved,
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
            release_info["artist"] = build_artist_credit_string(data["artist-credit"])

        for disc_index, media in enumerate(data.get("media", []), start=1):
            for track in media.get("tracks", []):
                recording = track.get("recording", {})

                release_info["tracks"].append({
                    "disc_number": disc_index,
                    "track_number": track.get("position"),
                    "title": track.get("title") or recording.get("title"),
                    "recording_mbid": recording.get("id"),
                    "duration": track.get("length"),
                })

        return release_info

    except Exception as e:
        logger.error("[MB RELEASE track fetch] %s", e, exc_info=True)
        return None


def lookup_musicbrainz_album(artist: str, album: str, existing_mbid: str = "") -> dict:
    """Look up an album on MusicBrainz and return release candidates."""
    svc = _get_service()
    matches = svc.search_releasegroup_matches(artist, album)
    return {"success": True, "candidates": matches, "existing_mbid": existing_mbid}


def get_release_group_releases(rg_mbid: str, include_track_counts: bool = False) -> dict:
    """Fetch all releases within a release group.

    Args:
        rg_mbid: MusicBrainz release-group MBID.
        include_track_counts: If True, also fetches track counts for each
            release via the releases browse endpoint (one extra API call).

    Returns:
        Dict with ``success`` and ``releases`` list.  Each release dict will
        include a ``track_count`` key when *include_track_counts* is True.
    """
    try:
        headers = {"User-Agent": "Popularr/1.0", "Accept": "application/json"}
        resp = httpx.get(
            f"https://musicbrainz.org/ws/2/release-group/{rg_mbid}",
            params={"fmt": "json", "inc": "releases"},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        releases = data.get("releases", [])

        if include_track_counts and releases:
            _enrich_releases_with_track_counts(releases)

        return {"success": True, "releases": releases}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _enrich_releases_with_track_counts(releases: list[dict]) -> None:
    """Mutate releases in-place, adding ``track_count`` from MusicBrainz.

    Uses the browse endpoint to fetch all releases for the first release's
    release-group with media info, then maps back by release MBID.
    """
    if not releases:
        return
    # Get the release-group MBID from the first release's release-group
    # (all releases in the list share the same release-group).
    rg_mbid = None
    first_rg = releases[0].get("release-group")
    if isinstance(first_rg, dict):
        rg_mbid = first_rg.get("id")
    if not rg_mbid:
        return

    try:
        headers = {"User-Agent": "Popularr/1.0", "Accept": "application/json"}
        resp = httpx.get(
            f"https://musicbrainz.org/ws/2/release/",
            params={
                "fmt": "json",
                "inc": "media",
                "release-group": rg_mbid,
                "limit": 100,
            },
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        browse_data = resp.json()
        # Build a lookup: release MBID → total track count
        tc_lookup: dict[str, int] = {}
        for rel in browse_data.get("releases", []):
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
    """Compare a MusicBrainz release group tracklist with the local library."""
    result = get_release_group_releases(rg_mbid)
    if not result.get("success"):
        return result
    return {"success": True, "artist": artist, "album": album, "releases": result.get("releases", [])}


def _get_local_track_count(artist: str, album: str) -> int:
    """Query the local database for the number of tracks in an album."""
    try:
        from db.engine import db_session
        from sqlalchemy import text
        with db_session() as session:
            result = session.execute(
                text("SELECT COUNT(*) AS cnt FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"),
                {"artist": artist, "album": album},
            )
            row = result.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


def get_musicbrainz_best_release(artist: str, album: str, rg_mbid: str) -> dict:
    """Find the best matching release inside a release group.

    Uses a combined score based on:
    - Title similarity to the local album name
    - Track-count proximity (when both local and MusicBrainz counts are known)
    - Official/Promotional status
    """
    result = get_release_group_releases(rg_mbid, include_track_counts=True)
    if not result.get("success"):
        return result
    releases = result.get("releases", [])
    if not releases:
        return {"success": False, "error": "No releases found"}

    local_tc = _get_local_track_count(artist, album)

    from difflib import SequenceMatcher
    scored = []
    for rel in releases:
        title = rel.get("title", "")
        title_score = SequenceMatcher(None, album.lower(), title.lower()).ratio()

        # Official releases get a bonus
        status_bonus = 0.1 if rel.get("status") == "Official" else 0.0

        # Track-count proximity: score 0.0–0.15 based on how close the
        # candidate's track count is to the local album's track count.
        tc_score = 0.0
        if local_tc > 0:
            candidate_tc = rel.get("track_count", 0) or 0
            if candidate_tc > 0:
                # Ratio of min to max -> 1.0 when equal, lower when mismatched
                ratio = min(local_tc, candidate_tc) / max(local_tc, candidate_tc)
                # Scale to at most 0.15 so it nudges rather than overrules
                tc_score = ratio * 0.15

        combined = title_score + status_bonus + tc_score
        scored.append({
            "release": rel,
            "score": round(combined, 3),
            "title_score": round(title_score, 3),
            "tc_score": round(tc_score, 3),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"success": True, "best": scored[0]["release"], "candidates": scored}

