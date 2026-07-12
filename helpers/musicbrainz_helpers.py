"""MusicBrainz UUID extraction helpers.

Provides:
- ``normalize_single_mbid`` – Extract one valid MBID from a multi-MBID string.
- ``get_mbid_from_metadata`` – Find first non-empty MBID across multiple dict keys.

These are pure string-processing functions with no HTTP or DB dependencies.
"""

import re

_MUSICBRAINZ_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)

def normalize_single_mbid(value: str) -> str:
    """Extract a single MusicBrainz UUID from a string that may contain multiple MBIDs.

    Some taggers write multiple artist MBIDs for collaboration tracks (e.g.
    "mbid1;mbid2" or "mbid1,mbid2").  This function finds the first valid UUID
    and returns it, ignoring the rest.

    Args:
        value: Raw MBID string, possibly containing multiple values.

    Returns:
        A single valid MusicBrainz UUID, or an empty string if none found.
    """
    if not value or not isinstance(value, str):
        return ""
    for part in re.split(r'[;|,\s/]+', value.strip()):
        part = part.strip()
        if _MUSICBRAINZ_UUID_RE.match(part):
            return part
    return ""


def get_mbid_from_metadata(metadata: dict, *keys: str) -> str:
    """Return the first non-empty string found in *metadata* under any of *keys*.

    Convenience helper for extracting a MusicBrainz ID from a metadata dict
    where the key may vary between callers or data sources.

    Args:
        metadata: Dict of track/release metadata.
        *keys: One or more possible key names to try (e.g. ``"release_mbid"`",
               ``"musicbrainz_albumid"``).

    Returns:
        The first non-empty value found, or empty string.
    """
    for key in keys:
        val = (metadata.get(key) or "").strip()
        if val:
            return val
    return ""


def fetch_writer_credits(title: str, artist: str) -> dict[str, list[str]]:
    """Fetch composer/writer/lyricist credits from MusicBrainz for a track.

    Uses the low-level HTTP client to search for the recording and extract
    artist-credit relationships.  Returns empty dict gracefully when the
    lookup fails or the data is unavailable.

    Args:
        title: Track title.
        artist: Track artist.

    Returns:
        Dict with keys ``composers``, ``writers``, ``lyricists`` (each a list).
    """
    try:
        from api_clients.musicbrainz_http import MusicBrainzHttpClient

        mb = MusicBrainzHttpClient()
        recordings = mb.search_recordings(f'artist:"{artist}" recording:"{title}"', limit=5)
        if not recordings:
            return {}

        recording = recordings[0]
        rec_id = recording.get("id", "") if isinstance(recording, dict) else ""
        if not rec_id:
            return {}

        # Fetch full recording details with relationships
        data = mb.get_recording(rec_id, inc="artists work-level-rels work-rels artist-rels")

        composers: list[str] = []
        writers: list[str] = []
        lyricists: list[str] = []

        for rel in data.get("relations", []):
            rel_type = (rel.get("type") or "").lower()
            target_type = (rel.get("target-type") or "").lower()
            if target_type != "work":
                continue
            work = rel.get("work", {})
            for work_rel in work.get("relations", []):
                wtype = (work_rel.get("type") or "").lower()
                ac = work_rel.get("artist-credit", [])
                for credit in ac:
                    name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
                    if name:
                        if wtype == "composer" and name not in composers:
                            composers.append(name)
                        elif wtype == "lyricist" and name not in lyricists:
                            lyricists.append(name)
                        elif wtype in ("writer", "text") and name not in writers:
                            writers.append(name)

        return {"composers": composers, "writers": writers, "lyricists": lyricists}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).debug(
            "Could not fetch writer credits for '%s' by '%s': %s", title, artist, exc)
        return {}


def _coerce_position_to_int(value, default):
    """Convert MusicBrainz position strings (e.g. 'A1', '1/12') into an integer."""
    raw = str(value or '').strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    match = re.search(r"\d+", raw)
    if match:
        return int(match.group(0))
    return default

def _build_artist_credit_string(artist_credit):
    """Build a display string from a MusicBrainz artist-credit array."""
    result = ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            result += credit.get('name', '')
            result += credit.get('joinphrase', '')
        else:
            result += str(credit)
    return result.strip()