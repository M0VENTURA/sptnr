# Implementation Guide: Exact Line Ranges for Extraction

## Files Created in Analysis
1. ✅ `ANALYSIS_SUMMARY.md` — Executive summary
2. ✅ `DETAILED_ANALYSIS.md` — Complete breakdown  
3. ✅ `FUNCTION_CALL_ANALYSIS.md` — Call chains & dependencies
4. ✅ `QUICK_REFERENCE.md` — Matrix tables

---

## Extract Candidates by Line Range (start.py)

### GROUP 1: Core Single Detection Functions (16 lines)
**Status:** Can move with circular import handling  
**Destination:** `single_detector.py`

#### is_discogs_single()
```
Location: Lines 1117-1124 (8 lines)
def is_discogs_single(
    title: str,
    artist: str,
    *,
    album_context: dict | None = None,
    timeout: int = 10
) -> bool:
    """Check if track is a single via Discogs (wrapper using DiscogsClient)."""
    return discogs_client.is_single(title, artist, album_context, timeout)
```

#### is_musicbrainz_single()
```
Location: Lines 1131-1134 (4 lines)
def is_musicbrainz_single(title: str, artist: str) -> bool:
    """Check if track is a single via MusicBrainz (wrapper using MusicBrainzClient)."""
    return musicbrainz_client.is_single(title, artist)
```

#### is_lastfm_single()
```
Location: Lines 1127-1130 (4 lines)
def is_lastfm_single(title: str, artist: str) -> bool:
    """Placeholder for Last.fm single detection."""
    return False
```

---

### GROUP 2: Video Detection Helpers (90 lines)
**Status:** Can move (only used by video detection)  
**Destination:** `single_detector.py`

#### _strip_video_noise()
```
Location: Lines 863-880 (18 lines)
def _strip_video_noise(s: str) -> str:
    """
    Remove common boilerplate to improve title matching:
      - 'official music video', 'official video', 'music video', 'hd', '4k', 'uhd', 'remastered'
      - bracketed content [..], (..), {..}
      - normalize 'feat.' / 'ft.' to 'feat '
    Returns a canonicalized string via _canon.
    """
    s = (s or "").lower()
    noise_phrases = [
        "official music video", "official video", "music video",
        "hd", "4k", "uhd", "remastered", "lyrics", "lyric video",
        "audio", "visualizer"
    ]
    for p in noise_phrases:
        s = s.replace(p, " ")
    # Drop bracketed content
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", s)
    # Normalize common abbreviations
    s = s.replace("feat.", "feat ").replace("ft.", "feat ")
    return _canon(s)
```

#### _has_official()
```
Location: Lines 881-891 (11 lines)
def _has_official(vt_raw: str, vd_raw: str, allow_lyric: bool = True) -> bool:
    """Require 'official' in title/description; optionally accept 'lyric' as official."""
    t = (vt_raw or "").lower()
    d = (vd_raw or "").lower()
    if ("official" in t) or ("official" in d):
        return True
    return allow_lyric and (("lyric" in t) or ("lyric" in d))
```

#### _banned_flavor()
```
Location: Lines 909-924 (16 lines)
def _banned_flavor(vt_raw: str, vd_raw: str, *, allow_live: bool = False) -> bool:
    """
    Reject 'live' and 'remix' unless allow_live=True.
    Radio edits are allowed (without 'remix').
    """
    t = (vt_raw or "").lower()
    d = (vd_raw or "").lower()

    # Live only banned when album context doesn't allow it
    if (not allow_live) and ("live" in t or "live" in d):
        return True

    # 'remix' anywhere is banned; radio edits allowed if no 'remix'
    if "remix" in t or "remix" in d:
        return True

    return False
```

#### _release_context_compatible_discogs()
```
Location: Lines 812-829 (18 lines)
def _release_context_compatible_discogs(rel_json: dict, require_live: bool, forbid_live: bool) -> bool:
    """Decide if a Discogs release is compatible with album context (live/unplugged)."""
    title_l = (rel_json.get("title") or "").lower()
    notes_l = (rel_json.get("notes") or "").lower()
    formats = rel_json.get("formats") or []
    tags = {d.lower() for f in formats for d in (f.get("descriptions") or [])}

    has_live_signal = (
        ("live" in tags) or ("unplugged" in title_l) or ("mtv unplugged" in title_l) or
        ("recorded live" in notes_l) or ("unplugged" in notes_l)
    )

    if require_live and not has_live_signal:
        return False
    if forbid_live and has_live_signal:
        return False
    return True
```

#### _release_context_compatible()
```
Location: Lines 831-836 (6 lines)
def _release_context_compatible(rel_json: dict, *, require_live: bool, forbid_live: bool) -> bool:
    """Generic wrapper to decide release context compatibility.
    Delegates to Discogs-specific implementation for now.
    """
    return _release_context_compatible_discogs(rel_json, require_live, forbid_live)
```

#### _has_official_on_release_top()
```
Location: Lines 838-855 (18 lines)
def _has_official_on_release_top(data: dict, nav_title: str, *, allow_live: bool, min_ratio: float = 0.50) -> bool:
    """Inspect release.videos for an 'official' (or 'lyric') match of nav_title."""
    vids = data.get("videos") or []
    nav_clean = _strip_video_noise(nav_title)
    for v in vids:
        vt_raw = (v.get("title") or "")
        vd_raw = (v.get("description") or "")
        if not _has_official(vt_raw, vd_raw, allow_lyric=True):
            continue
        if _banned_flavor(vt_raw, vd_raw, allow_live=allow_live):
            continue
        vt = _strip_video_noise(vt_raw)
        vd = _strip_video_noise(vd_raw)
        r = max(difflib.SequenceMatcher(None, vt, nav_clean).ratio(),
                difflib.SequenceMatcher(None, vd, nav_clean).ratio())
        if r >= min_ratio:
            return True
    return False
```

**Total for GROUP 2:** 90 lines (but includes 1 unused function)

---

### GROUP 3: Complex Video Signal Function (190 lines)
**Status:** Can move (only called by secondary_single_lookup and rate_artist)  
**Destination:** `single_detector.py`

#### discogs_official_video_signal()
```
Location: Lines 926-1115 (190 lines)

[ENTIRE FUNCTION - TOO LONG TO PASTE HERE]
See: start.py lines 926-1115

Includes:
- Main function definition (926-968)
- _inspect_release() nested helper (983-1012)
- Release search & shortlist (1018-1058)
- Parallel inspections (1060-1086)
- Permissive fallback (1088-1113)
```

**Dependencies to Import:**
- `_get_discogs_session()`
- `_throttle_discogs()`
- `_respect_retry_after()`
- `_strip_video_noise()`
- `_canon()`
- `_banned_flavor()`
- `_release_context_compatible_discogs()`
- `strip_parentheses()` (from helpers.py)
- `infer_album_context()`
- Global: `CONTEXT_GATE`, `CONTEXT_FALLBACK_STUDIO`, `_DISCOGS_VID_CACHE`, `_DEF_USER_AGENT`

---

### GROUP 4: Secondary Lookup Aggregator (53 lines)
**Status:** Can move (calls other single detection functions)  
**Destination:** `single_detector.py`

#### secondary_single_lookup()
```
Location: Lines 1136-1188 (53 lines)

def secondary_single_lookup(track: dict, artist_name: str, album_ctx: dict | None, *, singles_set: set | None = None, required_strong_sources: int = 2) -> dict:
    """Perform a lightweight secondary check for single evidence.

    Returns a dict: {"sources": [...], "confidence": "low|medium|high"}.
    This aggregates Discogs single/video, MusicBrainz, Last.fm, and Spotify prefetch signals.
    """
    sources = set()
    title = track.get("title", "")
    try:
        # Discogs single
        try:
            if DISCOGS_TOKEN and is_discogs_single(title, artist=artist_name, album_context=album_ctx):
                sources.add("discogs")
        except Exception:
            pass

        # Discogs official video
        try:
            if DISCOGS_TOKEN:
                dv = discogs_official_video_signal(title, artist_name, discogs_token=DISCOGS_TOKEN, album_context=album_ctx, permissive_fallback=CONTEXT_FALLBACK_STUDIO)
                if dv.get("match"):
                    sources.add("discogs_video")
        except Exception:
            pass

        # MusicBrainz
        try:
            if is_musicbrainz_single(title, artist_name):
                sources.add("musicbrainz")
        except Exception:
            pass

        # Last.fm (configurable)
        try:
            if config.get("features", {}).get("use_lastfm_single", True) and is_lastfm_single(title, artist_name):
                sources.add("lastfm")
        except Exception:
            pass

        # Spotify prefetch
        try:
            spid = track.get("spotify_id")
            if singles_set and spid and spid in singles_set:
                sources.add("spotify")
        except Exception:
            pass

    except Exception:
        return {"sources": [], "confidence": "low"}

    strong_sources = {"discogs", "discogs_video", "musicbrainz"}
    strong_count = len(sources & strong_sources)
    if strong_count >= required_strong_sources:
        confidence = "high"
    elif len(sources) >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return {"sources": sorted(sources), "confidence": confidence}
```

---

### GROUP 5: MUST STAY IN start.py

#### _canon() — Lines 694-700 (7 lines)
**Reason:** Used everywhere in title normalization (15+ locations)

#### _base_title() — Lines 704-709 (6 lines)
**Reason:** Used in rate_artist() line 2167

#### _has_subtitle_variant() — Lines 711-722 (12 lines)
**Reason:** Used in rate_artist() line 2168

#### _similar() — Lines 724-725 (2 lines)
**Reason:** Used in rate_artist() line 2169

#### infer_album_context() — Lines 893-908 (16 lines)
**Reason:** Used by BOTH rate_artist() (10+ places) AND single detection

#### All API Wrappers (50+ lines)
**Reason:** Required by external modules (popularity.py, mp3scanner.py, app.py)

---

## Step-by-Step Extraction Plan

### Step 1: Create `single_detector.py`
```python
#!/usr/bin/env python3
"""
Single Detection Module - Detects if tracks are singles vs album tracks.
Delegates to Discogs, Last.fm, MusicBrainz, and other sources.
"""

import re
import math
import logging
import difflib
from concurrent.futures import ThreadPoolExecutor

# Will be imported at runtime to avoid circular deps
# from start import (...)

def _strip_video_noise(s: str) -> str:
    # Lines 863-880 from start.py
    pass

# ... copy all helper functions here (90 lines)

def discogs_official_video_signal(...):
    # Lines 926-1115 from start.py
    pass

def is_discogs_single(...):
    # Lines 1117-1124 from start.py
    pass

def is_musicbrainz_single(...):
    # Lines 1131-1134 from start.py
    pass

def is_lastfm_single(...):
    # Lines 1127-1130 from start.py
    pass

def secondary_single_lookup(...):
    # Lines 1136-1188 from start.py
    pass
```

### Step 2: Update imports in functions
```python
# Inside secondary_single_lookup():
def secondary_single_lookup(track: dict, artist_name: str, album_ctx: dict | None, 
                           *, singles_set: set | None = None, 
                           required_strong_sources: int = 2) -> dict:
    # Lazy import to avoid circular dependency
    from start import (
        DISCOGS_TOKEN, CONTEXT_FALLBACK_STUDIO, config,
        discogs_official_video_signal, infer_album_context
    )
    # ... rest of function
```

### Step 3: Update start.py imports
Add at top of `rate_artist()` function:
```python
def rate_artist(artist_id, artist_name, verbose=False, force=False):
    # Lazy import to get detection functions
    from single_detector import (
        is_discogs_single, is_lastfm_single, 
        is_musicbrainz_single, secondary_single_lookup,
        discogs_official_video_signal
    )
    # ... rest of function (no changes needed)
```

### Step 4: Remove from start.py
Delete lines:
- 812-829: `_release_context_compatible_discogs()`
- 831-836: `_release_context_compatible()`
- 838-855: `_has_official_on_release_top()`
- 863-880: `_strip_video_noise()`
- 881-891: `_has_official()`
- 909-924: `_banned_flavor()`
- 926-1115: `discogs_official_video_signal()`
- 1117-1124: `is_discogs_single()`
- 1127-1130: `is_lastfm_single()`
- 1131-1134: `is_musicbrainz_single()`
- 1136-1188: `secondary_single_lookup()`

### Step 5: Update singledetection.py
Change:
```python
# OLD:
from start import (
    is_discogs_single,
    is_lastfm_single,
    is_musicbrainz_single,
    secondary_single_lookup,
    infer_album_context,
)

# NEW:
from start import infer_album_context
from single_detector import (
    is_discogs_single,
    is_lastfm_single,
    is_musicbrainz_single,
    secondary_single_lookup,
)
```

### Step 6: Test
```bash
# Test single detection module
python singledetection.py --verbose

# Test rating (calls single detection internally)
python start.py rate --artist "Test Artist"

# Test popularity (uses API wrappers)
python popularity.py

# Test MP3 scanner
python mp3scanner.py
```

---

## Risk Mitigation Checklist

- [ ] Create backup of original start.py
- [ ] Run all existing tests before changes
- [ ] Create single_detector.py with copied code
- [ ] Verify imports work (lazy imports in functions)
- [ ] Test singledetection.py in isolation
- [ ] Test rate_artist() for 1-2 artists
- [ ] Test popularity.py with existing DB
- [ ] Test mp3scanner.py with existing DB
- [ ] Verify no regression in rating accuracy
- [ ] Check for circular import warnings
- [ ] Validate all external module imports still work

---

## Success Criteria

✅ All tests pass  
✅ No circular import errors  
✅ `start.py` reduced from 3,114 to ~2,768 lines  
✅ `single_detector.py` created (346 lines)  
✅ All functionality preserved  
✅ No breaking changes to external APIs  
✅ Code quality maintained or improved  
