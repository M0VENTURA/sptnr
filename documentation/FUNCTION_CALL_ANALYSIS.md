# Detailed Function Analysis: Call Chains & Dependencies

## Function Call Chain: rate_artist() → Single Detection

### rate_artist() Single Detection Block (lines 2164-2350+)
```python
rate_artist(artist_id, artist_name, verbose=False, force=False):
    # ... setup code ...
    
    # Line 2164-2172: Title variant analysis
    canonical_base = _base_title(title)              # Line 2167 ← MUST STAY in start.py
    sim_to_base = _similar(title, canonical_base)   # Line 2169 ← MUST STAY in start.py
    has_subtitle = _has_subtitle_variant(title)     # Line 2168 ← MUST STAY in start.py
    
    # Line 2176: Discogs single check
    is_discogs = is_discogs_single(...)             # ← CAN MOVE to single_detector.py
    
    # Line 2193: MusicBrainz single check
    is_mb = is_musicbrainz_single(...)              # ← CAN MOVE to single_detector.py
    
    # Line 2212: Discogs video check
    dv_result = discogs_official_video_signal(...)  # ← SHOULD STAY in start.py (190 lines)
    
    # Line 2277: Last.fm single check
    is_lastfm = is_lastfm_single(...)               # ← CAN MOVE to single_detector.py
    
    # Line 2338: Secondary lookup (aggregates above)
    secondary_lookup = secondary_single_lookup(...) # ← CAN MOVE to single_detector.py
```

---

## Detailed Function Dependencies

### secondary_single_lookup() — Lines 1136-1188 (53 lines)

**Definition:**
```python
def secondary_single_lookup(track: dict, artist_name: str, album_ctx: dict | None, 
                           *, singles_set: set | None = None, 
                           required_strong_sources: int = 2) -> dict:
```

**Calls (Internal):**
- Line 1145: `is_discogs_single()` — 8 lines, same file
- Line 1154: `discogs_official_video_signal()` — 190 lines, same file
- Line 1159: `is_musicbrainz_single()` — 4 lines, same file
- Line 1169: `is_lastfm_single()` — 4 lines, same file

**Uses Global Vars:**
- `DISCOGS_TOKEN` (line 1145, 1154)
- `CONTEXT_FALLBACK_STUDIO` (line 1154)
- `config` (line 1167)

**Return Value:** `{"sources": [...], "confidence": "low|medium|high"}`

**Callers:**
1. `singledetection.py` line 117 — External script
2. `start.py` line 2338 — Inside `rate_artist()`

**Can Move?** ✅ **YES** — But must move all 4 functions it calls, or keep as imports

---

### discogs_official_video_signal() — Lines 926-1115 (190 lines)

**Definition:**
```python
def discogs_official_video_signal(
    title: str, artist: str, *,
    discogs_token: str, timeout: int = 10, per_page: int = 10,
    min_ratio: float = 0.55, allow_lyric_as_official: bool = True,
    album_context: dict | None = None, permissive_fallback: bool = False,
) -> dict:
```

**Internal Dependencies (called/used):**
- Line 957: `_get_discogs_session()` — 10 lines
- Line 960: `_throttle_discogs()` — 9 lines (within helper)
- Line 963: `strip_parentheses()` — from helpers.py
- Line 964: `_strip_video_noise()` — 18 lines
- Line 967: `_canon()` — 7 lines
- Line 973-975: `CONTEXT_GATE` (global)
- Line 983: `_inspect_release()` (nested helper, 35+ lines)
  - Uses: `_throttle_discogs()`, `_respect_retry_after()`, `_release_context_compatible_discogs()`, `_banned_flavor()`, `_strip_video_noise()`, `_has_official()`
- Line 1018: `_discogs_search()` — 16 lines
- Line 1053: `_release_context_compatible_discogs()` — 18 lines
- Line 1083+: ThreadPoolExecutor parallel processing
- Line 1117: `_DISCOGS_VID_CACHE` (global dict)

**Callers:**
1. `secondary_single_lookup()` line 1154
2. `start.py` line 2212 (inside `rate_artist()`)

**Can Move?** ⚠️ **COMPLEX** — Has 8+ nested and local dependencies, but all are helpers ONLY used by this function

---

### Helper Function: _strip_video_noise() — Lines 863-880 (18 lines)

**Definition:**
```python
def _strip_video_noise(s: str) -> str:
    """Remove common boilerplate: 'official music video', 'official video', etc."""
```

**Calls:**
- Line 879: `_canon()` — 7 lines

**Used by:**
- `_has_official_on_release_top()` line 839
- `discogs_official_video_signal()` nested (2 locations)

**Can Move?** ✅ **YES** — Only video detection helpers use it

---

### Helper Function: _banned_flavor() — Lines 909-924 (16 lines)

**Definition:**
```python
def _banned_flavor(vt_raw: str, vd_raw: str, *, allow_live: bool = False) -> bool:
    """Reject 'live' and 'remix' unless allow_live=True."""
```

**Calls:** None (pure logic)

**Used by:**
- `_has_official_on_release_top()` line 850
- `discogs_official_video_signal()` nested (3 locations)

**Can Move?** ✅ **YES** — Only video detection uses it

---

### Helper Function: _release_context_compatible_discogs() — Lines 812-829 (18 lines)

**Definition:**
```python
def _release_context_compatible_discogs(rel_json: dict, require_live: bool, forbid_live: bool) -> bool:
    """Decide if Discogs release is compatible with album context."""
```

**Calls:** None (pure logic)

**Used by:**
- `_release_context_compatible()` line 834
- `discogs_official_video_signal()` nested (2 locations)

**Can Move?** ✅ **YES** — Only video/single detection uses it

---

### Helper Function: _has_official() — Lines 881-891 (11 lines)

**Definition:**
```python
def _has_official(vt_raw: str, vd_raw: str, allow_lyric: bool = True) -> bool:
    """Require 'official' or 'lyric' in title/description."""
```

**Calls:** None (pure logic)

**Used by:**
- `_has_official_on_release_top()` line 845
- `discogs_official_video_signal()` nested

**Can Move?** ✅ **YES** — Only video detection uses it

---

### Helper Function: _has_official_on_release_top() — Lines 838-855 (18 lines)

**Definition:**
```python
def _has_official_on_release_top(data: dict, nav_title: str, *, allow_live: bool, min_ratio: float = 0.50) -> bool:
    """Inspect release.videos for official (or lyric) match."""
```

**Calls:**
- Line 840: `_strip_video_noise()`
- Line 845: `_has_official()`
- Line 850: `_banned_flavor()`

**Used by:** **NONE** (appears unused/legacy)

**Can Move?** ✅ **YES** — Unused, can be archived

---

## Functions That MUST Stay in start.py

### _canon() — Lines 694-700 (7 lines)
```python
def _canon(s: str) -> str:
    """Lowercase, strip parentheses and punctuation, normalize whitespace."""
```

**Used by (15+ locations):**
- `_base_title()` (indirectly via `_strip_video_noise()`)
- `_similar()` (definition)
- `_release_title_core()` (definition)
- `_is_variant_of()` (definition)
- `_strip_video_noise()` (definition)
- `infer_album_context()` — NO
- `discogs_official_video_signal()` (line 967, 1019)
- Multiple title matching operations in `rate_artist()`

**Can Move?** ❌ **NO** — Too widely used across entire module

---

### infer_album_context() — Lines 893-908 (16 lines)
```python
def infer_album_context(album_title: str, release_types: list[str] | None = None) -> dict:
    """Infer album context flags (live/unplugged)."""
    # Returns: {"is_live": bool, "is_unplugged": bool, "title": str, "raw_types": list}
```

**Used by:**
1. `secondary_single_lookup()` (line 1138)
2. `rate_artist()` (at least 10 locations: album_ctx usage throughout)
3. `discogs_official_video_signal()` (line 946)
4. `singledetection.py` (imported and used)

**Can Move?** ❌ **NO** — Shared between `rate_artist()` and single detection

---

## Summary: Safe vs. Risky Moves

### ✅ SAFE TO MOVE (No Cross-Module Usage)
| Function | Lines | Risk |
|----------|-------|------|
| `is_lastfm_single()` | 4 | 🟢 None (placeholder) |
| `_has_official_on_release_top()` | 18 | 🟢 None (unused) |
| `_banned_flavor()` | 16 | 🟢 None (video-only) |
| `_has_official()` | 11 | 🟢 None (video-only) |
| `_release_context_compatible()` | 6 | 🟢 None (video-only) |
| `_release_context_compatible_discogs()` | 18 | 🟢 None (video-only) |
| `_strip_video_noise()` | 18 | 🟢 None (video-only) |

**Total Safe:** 91 lines

---

### ⚠️ CAN MOVE WITH IMPORT (Shared But Moveable)
| Function | Lines | Dependencies | Risk |
|----------|-------|--------------|------|
| `is_discogs_single()` | 8 | `discogs_client` (global) | 🟡 Medium |
| `is_musicbrainz_single()` | 4 | `musicbrainz_client` (global) | 🟡 Medium |
| `secondary_single_lookup()` | 53 | All 4 single functions + globals | 🟡 Medium |
| `discogs_official_video_signal()` | 190 | 8+ helpers + globals | 🟡 Medium |

**Total Moveable:** 255 lines

---

### ❌ MUST STAY (Shared With Rate Logic)
| Function | Lines | Used by rate_artist? |
|----------|-------|----------------------|
| `_canon()` | 7 | ✅ YES (many places) |
| `_base_title()` | 6 | ✅ YES (line 2167) |
| `_has_subtitle_variant()` | 12 | ✅ YES (line 2168) |
| `_similar()` | 2 | ✅ YES (line 2169) |
| `infer_album_context()` | 16 | ✅ YES (10+ places) |
| All API wrappers | ~50 | ✅ YES + external modules |

**Total Stay:** ~93 lines

---

## Recommended Move Set

**Move to `single_detector.py`:**
```
secondary_single_lookup()                    53 lines
is_discogs_single()                          8 lines
is_musicbrainz_single()                      4 lines
is_lastfm_single()                           4 lines
_strip_video_noise()                         18 lines
_banned_flavor()                             16 lines
_has_official()                              11 lines
_has_official_on_release_top()               18 lines
_release_context_compatible()                6 lines
_release_context_compatible_discogs()        18 lines
discogs_official_video_signal()              190 lines
───────────────────────────────────────────────────
TOTAL:                                       346 lines
```

**Keep in `start.py`:**
- Core utilities: `_canon()`, `_base_title()`, `_has_subtitle_variant()`, `_similar()`
- Shared context: `infer_album_context()`
- All API wrappers and external dependencies
- `enrich_genres_aggressively()` (genre logic)
- All other rating-specific code

**Result:**
- `start.py` reduced from 3,114 to ~2,768 lines (346 freed)
- New `single_detector.py` created (346 lines, focused)
- Circular import managed via lazy imports
