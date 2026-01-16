# SPTNR Code Refactoring Analysis: start.py Modularization

**Analysis Date:** January 2, 2026  
**Target File:** `start.py` (3,114 lines)  
**Objective:** Identify functions for safe modularization into `singledetection.py`

---

## 1. TARGET FUNCTIONS ANALYSIS

### A. Primary Single Detection Functions

#### `is_discogs_single()` — Lines 1117-1124 (8 lines)
```python
def is_discogs_single(title: str, artist: str, *, album_context: dict | None = None, timeout: int = 10) -> bool:
    """Check if track is a single via Discogs (wrapper using DiscogsClient)."""
    return discogs_client.is_single(title, artist, album_context, timeout)
```
- **Type:** API wrapper (thin wrapper around `DiscogsClient`)
- **Dependencies:** `discogs_client` (global), `album_context` dict
- **Usage:** `singledetection.py` (78, 91), `start.py` (1146, 2176)
- **Used by:** `secondary_single_lookup()`, `rate_artist()`
- **Can Move:** ✅ **YES** — but `DiscogsClient` instance must be accessible

---

#### `is_lastfm_single()` — Lines 1127-1130 (4 lines)
```python
def is_lastfm_single(title: str, artist: str) -> bool:
    """Placeholder for Last.fm single detection."""
    return False
```
- **Type:** Placeholder (non-functional)
- **Dependencies:** None
- **Usage:** `singledetection.py` (91), `start.py` (1169, 2277)
- **Used by:** `secondary_single_lookup()`, `rate_artist()`
- **Can Move:** ✅ **YES** — trivial stub

---

#### `is_musicbrainz_single()` — Lines 1131-1134 (4 lines)
```python
def is_musicbrainz_single(title: str, artist: str) -> bool:
    """Check if track is a single via MusicBrainz (wrapper using MusicBrainzClient)."""
    return musicbrainz_client.is_single(title, artist)
```
- **Type:** API wrapper (thin wrapper around `MusicBrainzClient`)
- **Dependencies:** `musicbrainz_client` (global)
- **Usage:** `singledetection.py` (78, 91), `start.py` (1159, 2193)
- **Used by:** `secondary_single_lookup()`, `rate_artist()`
- **Can Move:** ✅ **YES** — but `MusicBrainzClient` instance must be accessible

---

#### `secondary_single_lookup()` — Lines 1136-1188 (53 lines)
```python
def secondary_single_lookup(track: dict, artist_name: str, album_ctx: dict | None, 
                           *, singles_set: set | None = None, required_strong_sources: int = 2) -> dict:
    """Perform a lightweight secondary check for single evidence."""
    # ... (calls: is_discogs_single, discogs_official_video_signal, 
    #            is_musicbrainz_single, is_lastfm_single)
```
- **Type:** Composite single detection aggregator
- **Dependencies:**
  - `is_discogs_single()` (local)
  - `discogs_official_video_signal()` (local)
  - `is_musicbrainz_single()` (local)
  - `is_lastfm_single()` (local)
  - `config` (global)
  - `DISCOGS_TOKEN` (global)
  - `CONTEXT_FALLBACK_STUDIO` (global)
- **Usage:** `singledetection.py` (117), `start.py` (2338)
- **Used by:** `rate_artist()` (single usage in start.py, one in singledetection.py)
- **Can Move:** ✅ **YES** — but depends on all 4 single-check functions below it

---

### B. Video Detection Function

#### `discogs_official_video_signal()` — Lines 926-1115 (190 lines)
```python
def discogs_official_video_signal(title: str, artist: str, *, discogs_token: str, ...) -> dict:
    """Detect an 'official' (or 'lyric' if allowed) video for a track on Discogs"""
    # ... (complex logic with thread pool, nested helpers, caching)
```
- **Type:** Complex Discogs video detection with caching and parallel API calls
- **Dependencies:**
  - `_get_discogs_session()` (local)
  - `_throttle_discogs()` (local)
  - `_respect_retry_after()` (local)
  - `_strip_video_noise()` (local)
  - `_canon()` (local)
  - `_banned_flavor()` (local)
  - `_release_context_compatible_discogs()` (local)
  - `strip_parentheses()` (from helpers.py)
  - `infer_album_context()` (local)
  - `CONTEXT_GATE` (global)
  - `CONTEXT_FALLBACK_STUDIO` (global)
  - `_DEF_USER_AGENT` (global)
  - `_DISCOGS_VID_CACHE` (global dict)
- **Usage:** `start.py` (1154, 2212), `secondary_single_lookup()` (1154)
- **Used by:** `secondary_single_lookup()`, `rate_artist()`
- **Can Move:** ⚠️ **COMPLEX** — Depends on ~8 helper functions. Should move as a group.

---

#### `infer_album_context()` — Lines 893-908 (16 lines)
```python
def infer_album_context(album_title: str, release_types: list[str] | None = None) -> dict:
    """Infer album context flags (live/unplugged) from album title"""
```
- **Type:** Context analysis helper
- **Dependencies:** None
- **Usage:** `singledetection.py` (27), `start.py` (1754, 1828, 1841, 2057, 2213, plus many in `rate_artist()`)
- **Used by:** `secondary_single_lookup()`, `rate_artist()`, `is_discogs_single()`, `discogs_official_video_signal()`, `singledetection.py`
- **Can Move:** ⚠️ **SHARED** — Used by both `rate_artist()` and single detection. Better to keep in start.py as a shared utility.

---

### C. Helper Functions for Video Detection

#### `_strip_video_noise()` — Lines 863-880 (18 lines)
- **Type:** String normalization helper
- **Dependencies:** `_canon()` (local)
- **Used by:** `_has_official_on_release_top()`, `discogs_official_video_signal()` (2x)
- **Can Move:** ⚠️ **Indirect** — Only via video functions

#### `_banned_flavor()` — Lines 909-924 (16 lines)
- **Type:** Content filtering helper
- **Dependencies:** None
- **Used by:** `_has_official_on_release_top()`, `discogs_official_video_signal()` (3x)
- **Can Move:** ⚠️ **Indirect** — Only via video functions

#### `_release_context_compatible_discogs()` — Lines 812-829 (18 lines)
- **Type:** Discogs release filtering helper
- **Dependencies:** None
- **Used by:** `_release_context_compatible()`, `discogs_official_video_signal()`
- **Can Move:** ⚠️ **Indirect** — Only via video functions

#### `_release_context_compatible()` — Lines 831-836 (6 lines)
- **Type:** Generic wrapper (currently delegates to Discogs-specific)
- **Dependencies:** `_release_context_compatible_discogs()` (local)
- **Used by:** `discogs_official_video_signal()`
- **Can Move:** ⚠️ **Indirect** — Only via video functions

#### `_has_official_on_release_top()` — Lines 838-855 (18 lines)
- **Type:** Video title matching helper
- **Dependencies:** `_strip_video_noise()`, `_has_official()`, `_banned_flavor()`
- **Used by:** **NONE in current codebase** (appears to be legacy)
- **Can Move:** ✅ **YES** — Unused, can be archived

#### `_has_official()` — Lines 881-891 (11 lines)
- **Type:** Official marker detection
- **Dependencies:** None
- **Used by:** `_has_official_on_release_top()`, `discogs_official_video_signal()` (nested)
- **Can Move:** ⚠️ **Indirect** — Only via video functions

#### `_canon()` — Lines 694-700 (7 lines)
- **Type:** Core normalization utility
- **Dependencies:** `re` (stdlib)
- **Used by:** **MANY** locations throughout start.py (title comparison, release matching, etc.)
- **Can Move:** ❌ **NO** — Too widely used (at least 15+ locations in `rate_artist()` alone)

#### `_base_title()` — Lines 704-709 (6 lines)
- **Type:** Title parsing helper
- **Dependencies:** `re` (stdlib)
- **Used by:** `rate_artist()` (line 2167)
- **Can Move:** ❌ **NO** — Used by `rate_artist()` for canonical title detection

#### `_has_subtitle_variant()` — Lines 711-722 (12 lines)
- **Type:** Title variation detection
- **Dependencies:** `re` (stdlib)
- **Used by:** `rate_artist()` (line 2168)
- **Can Move:** ❌ **NO** — Used by `rate_artist()`

#### `_similar()` — Lines 724-725 (2 lines)
- **Type:** Similarity wrapper
- **Dependencies:** `_canon()` (local), `difflib` (stdlib)
- **Used by:** `rate_artist()` (line 2169), many Discogs matching operations
- **Can Move:** ❌ **NO** — Too widely used in rating logic

---

### D. Discogs Session Management (Shared with Rating)

#### `_get_discogs_session()` — Lines 755-764
- **Type:** HTTP session factory
- **Used by:** `discogs_official_video_signal()`, `_discogs_search()`
- **Used by `rate_artist()`?** Unknown (search via Discogs within rating)
- **Can Move:** ⚠️ **SHARED** — Used by both single detection and potentially rating

#### `_throttle_discogs()` — Lines 771-779
- **Type:** Rate limiter
- **Used by:** `_discogs_search()`, `discogs_official_video_signal()`
- **Can Move:** ⚠️ **SHARED** — Used by both detection and potentially rating

#### `_respect_retry_after()` — Lines 782-790
- **Type:** Retry handler
- **Used by:** `_discogs_search()`, `discogs_official_video_signal()`
- **Can Move:** ⚠️ **SHARED** — Used by both detection and potentially rating

#### `_discogs_search()` — Lines 794-809
- **Type:** Discogs API query helper
- **Used by:** `discogs_official_video_signal()`
- **Can Move:** ⚠️ **SHARED** — Likely used by rating logic for album lookups

---

## 2. EXTERNAL MODULE IMPORTS ANALYSIS

### popularity.py Imports from start.py:
```python
from start import (
    get_spotify_artist_id,        # API wrapper ✅ STAY (Spotify wrapper)
    search_spotify_track,         # API wrapper ✅ STAY (Spotify wrapper)
    get_lastfm_track_info,        # API wrapper ✅ STAY (Last.fm wrapper)
    get_listenbrainz_score,       # API wrapper ✅ STAY (ListenBrainz wrapper)
    score_by_age,                 # Scoring logic ✅ STAY (used for aging tracks)
)
```
**Verdict:** All are **API wrappers** → MUST STAY in start.py

---

### mp3scanner.py Imports from start.py:
```python
from start import get_suggested_mbid  # Line 319
```
**Function:** Lines 1197-1199 (3 lines, simple wrapper around `MusicBrainzClient`)  
**Verdict:** API wrapper → MUST STAY in start.py

---

### singledetection.py Imports from start.py:
```python
from start import (
    is_discogs_single,           # Single detection ✅ COULD MOVE (but circ. dep.)
    is_lastfm_single,            # Single detection ✅ COULD MOVE
    is_musicbrainz_single,       # Single detection ✅ COULD MOVE
    secondary_single_lookup,     # Single detection ✅ COULD MOVE
    infer_album_context,         # Shared utility ⚠️ KEEP (also used by rate_artist)
)
```
**Note:** `singledetection.py` currently imports FROM start.py, so moving these functions OUT creates circular dependency risk.

---

## 3. RATE_ARTIST USAGE ANALYSIS

**Function:** Lines 1783-2733 (950+ lines)

**Direct calls to single detection functions:**
- Line 2176: `is_discogs_single()`
- Line 2212: `discogs_official_video_signal()`
- Line 2277: `is_lastfm_single()`
- Line 2193: `is_musicbrainz_single()`
- Line 2338: `secondary_single_lookup()`

**Verdict:** `rate_artist()` uses single detection extensively — cannot move those functions without refactoring the caller.

---

## 4. HELPER FUNCTION DEPENDENCY TREE

```
discogs_official_video_signal (190 lines)
├── _get_discogs_session()
├── _strip_video_noise(18 lines)
│   └── _canon(7 lines)
├── _canon(7 lines)
├── _banned_flavor(16 lines)
├── _has_official(11 lines)
├── _release_context_compatible_discogs(18 lines)
├── strip_parentheses() [from helpers.py]
├── infer_album_context(16 lines)
├── CONTEXT_GATE (global)
├── CONTEXT_FALLBACK_STUDIO (global)
└── _DISCOGS_VID_CACHE (global)

secondary_single_lookup(53 lines)
├── is_discogs_single(8 lines)
├── discogs_official_video_signal(190 lines) [see above]
├── is_musicbrainz_single(4 lines)
├── is_lastfm_single(4 lines)
├── config (global)
├── DISCOGS_TOKEN (global)
└── CONTEXT_FALLBACK_STUDIO (global)
```

---

## 5. SAFE MOVE SUMMARY

### ✅ FUNCTIONS SAFE TO MOVE TO singledetection.py:

| Function | Lines | Only Single Detection? | Risk Level |
|----------|-------|------------------------|-----------|
| `is_lastfm_single()` | 4 | ✅ YES | 🟢 NONE |
| `_has_official_on_release_top()` | 18 | ✅ YES (unused) | 🟢 NONE |
| `_release_title_core()` | 8 | ❓ Check `rate_artist()` | 🟡 LOW |
| `_is_variant_of()` | 9 | ❓ Check `rate_artist()` | 🟡 LOW |

**Total Saveable (no risk):** ~31 lines (but minimal impact)

---

### ⚠️ FUNCTIONS THAT COULD MOVE BUT REQUIRE REFACTORING:

| Function | Lines | Issue | Mitigation |
|----------|-------|-------|-----------|
| `is_discogs_single()` | 8 | Used in `rate_artist()` | Import from singledetection module after initialization |
| `is_musicbrainz_single()` | 4 | Used in `rate_artist()` | Import from singledetection module after initialization |
| `secondary_single_lookup()` | 53 | Calls other single functions | Move entire suite together |
| `discogs_official_video_signal()` | 190 | Complex; 8 dependencies | Move with full helper suite (saves ~450 lines total) |

---

### ❌ FUNCTIONS THAT MUST STAY IN start.py:

| Function | Lines | Reason | Uses in start.py |
|----------|-------|--------|------------------|
| `infer_album_context()` | 16 | Used by both `rate_artist()` AND single detection | 10+ locations |
| `_canon()` | 7 | Core utility for ALL title comparisons | 15+ locations |
| `_base_title()` | 6 | Used by `rate_artist()` | Line 2167 |
| `_has_subtitle_variant()` | 12 | Used by `rate_artist()` | Line 2168 |
| `_similar()` | 2 | Used by `rate_artist()` + Discogs matching | Many |
| `_get_discogs_session()` | 10 | May be used by rating logic | Unclear |
| `_throttle_discogs()` | 9 | Rate limiter (shared) | Unclear |
| `_respect_retry_after()` | 9 | Retry handler (shared) | Unclear |
| `_discogs_search()` | 16 | Discogs API queries (shared) | Unclear |
| All API wrappers | ~50 | Required by `popularity.py`, `app.py`, others | External |
| `enrich_genres_aggressively()` | 50 | Genre enrichment in `rate_artist()` | Line 1869 |

---

## 6. RECOMMENDED REFACTORING STRATEGY

### Option A: MINIMAL MOVE (LOW RISK, 31 lines freed)
**Move to singledetection.py:**
- `is_lastfm_single()` (4 lines) — unused stub
- `_has_official_on_release_top()` (18 lines) — unused legacy function
- One or two other minor helpers

**Savings:** ~31 lines  
**Effort:** Minimal  
**Risk:** 🟢 None (already isolated in singledetection.py)

---

### Option B: MEDIUM MOVE (MODERATE RISK, ~280 lines freed)
**Move to singledetection.py as a package:**
- `secondary_single_lookup()` (53 lines)
- `is_discogs_single()` (8 lines)
- `is_musicbrainz_single()` (4 lines)
- `is_lastfm_single()` (4 lines)
- Helper suite for video detection:
  - `_strip_video_noise()` (18 lines)
  - `_banned_flavor()` (16 lines)
  - `_has_official()` (11 lines)
  - `_release_context_compatible_discogs()` (18 lines)
  - `_release_context_compatible()` (6 lines)
  - `_has_official_on_release_top()` (18 lines) [unused]

**NOT Moved (keep as shared utilities):**
- `infer_album_context()` — used by both modules
- `_canon()` — core utility
- `discogs_official_video_signal()` — keep in start.py (too large; imports it)

**Savings:** ~156 lines (excluding video signal)  
**Effort:** Moderate (requires importing from singledetection module in start.py)  
**Risk:** 🟡 Medium (circular import if singledetection.py also imports start.py)

**Circular Dependency Solution:**
```python
# In singledetection.py:
def run_single_detection():
    from start import (
        discogs_official_video_signal, 
        infer_album_context,
        CONTEXT_GATE, CONTEXT_FALLBACK_STUDIO
    )
    # ... use them
```

---

### Option C: AGGRESSIVE MOVE (HIGH RISK, ~450 lines freed)
**Move entire single detection suite to new module `single_detector.py`:**
- `discogs_official_video_signal()` (190 lines) — complex, large
- All 9 helper functions (100+ lines)
- `secondary_single_lookup()` (53 lines)
- Single check wrappers (16 lines)

**Shared utilities stay in start.py:**
- `infer_album_context()` (16 lines) — imported as needed
- `_canon()` (7 lines) — imported as needed

**Savings:** ~450 lines  
**Effort:** High (requires careful dependency injection)  
**Risk:** 🔴 High (refactoring `rate_artist()` to inject detection function)

---

## 7. CURRENT ARCHITECTURE LIMITATIONS

### Why AGGRESSIVE MOVE is risky:
1. **Circular Imports:** `singledetection.py` imports from `start.py`, but if `start.py` moves functions to a new module and imports back, creates complexity
2. **Global State:** `_DISCOGS_VID_CACHE`, `CONTEXT_GATE`, `DISCOGS_TOKEN` are global — need to be passed or injected
3. **API Client Access:** Functions need `discogs_client`, `musicbrainz_client` — must be instantiated before use
4. **Concurrent Usage:** `rate_artist()` and `singledetection.py` both may call single detection — thread safety needed

---

## 8. FINAL VERDICT & RECOMMENDATIONS

### Functions SAFE to Move Immediately (No Risk):
- `is_lastfm_single()` — 4 lines (stub)
- `_has_official_on_release_top()` — 18 lines (unused)

**Total Savings:** 22 lines ✅

---

### Functions SAFE to Move with Minor Refactoring (Low-Medium Risk):
- `secondary_single_lookup()` — 53 lines
- `is_discogs_single()` — 8 lines
- `is_musicbrainz_single()` — 4 lines
- Video detection helpers (10 functions) — ~110 lines

**Total Savings:** ~175 lines  
**Effort:** Move to new `single_detector.py` module; import selectively in `rate_artist()`

---

### Functions MUST STAY (Shared Usage):
- `infer_album_context()` — used by both `rate_artist()` and `single_detector.py`
- `_canon()` — core utility everywhere
- `_base_title()`, `_has_subtitle_variant()`, `_similar()` — used in `rate_artist()`
- `discogs_official_video_signal()` — can stay or move, but large + complex
- All API wrappers (`get_spotify_*`, `search_spotify_*`, `get_lastfm_*`, etc.) — required by external modules

---

## 9. ESTIMATED LINE COUNT FREED UP

| Option | Lines Freed | Effort | Risk |
|--------|------------|--------|------|
| **Minimal (Option A)** | 22 lines | 1 hour | 🟢 None |
| **Medium (Option B)** | 175 lines | 4-6 hours | 🟡 Medium |
| **Aggressive (Option C)** | 450 lines | 12-16 hours | 🔴 High |

---

## 10. RECOMMENDED ACTION

**PROCEED WITH OPTION B (Medium Move):**

1. **Create `single_detector.py`** module with:
   - `secondary_single_lookup()`
   - Single detection wrappers
   - Video detection helpers
   - Total: ~175 lines

2. **Keep in start.py:**
   - `discogs_official_video_signal()` (large, complex, imported by others)
   - Utility functions (`_canon`, `_base_title`, `infer_album_context`)
   - All API wrappers

3. **Update imports:**
   - `singledetection.py` → import from both `start.py` (API wrappers) and `single_detector.py` (detection logic)
   - `rate_artist()` → import `secondary_single_lookup()` from `single_detector.py`

4. **Result:**
   - ~175 lines removed from start.py
   - New `single_detector.py` module created (focused, testable)
   - Better separation of concerns
   - Minimal circular import risk
