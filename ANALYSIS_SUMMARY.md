# SPTNR start.py Analysis - Executive Summary

## Overview
Analyzed `start.py` (3,114 lines) to identify functions that could be safely moved to separate modules.

---

## Functions Found & Line Ranges

### Single Detection Functions
| Function | Lines | Defined |
|----------|-------|---------|
| `is_discogs_single()` | 1117-1124 | 8 lines |
| `is_lastfm_single()` | 1127-1130 | 4 lines |
| `is_musicbrainz_single()` | 1131-1134 | 4 lines |
| `secondary_single_lookup()` | 1136-1188 | 53 lines |

### Video Detection
| Function | Lines | Defined |
|----------|-------|---------|
| `discogs_official_video_signal()` | 926-1115 | 190 lines |
| `infer_album_context()` | 893-908 | 16 lines |

### Video Helper Functions
| Function | Lines | Defined |
|----------|-------|---------|
| `_strip_video_noise()` | 863-880 | 18 lines |
| `_banned_flavor()` | 909-924 | 16 lines |
| `_has_official()` | 881-891 | 11 lines |
| `_has_official_on_release_top()` | 838-855 | 18 lines |
| `_release_context_compatible()` | 831-836 | 6 lines |
| `_release_context_compatible_discogs()` | 812-829 | 18 lines |

---

## Usage Summary

### Single Detection Usage
| Function | singledetection.py | rate_artist() | Other |
|----------|-------------------|---------------|-------|
| `is_discogs_single()` | ✅ Import (used) | ✅ Used | - |
| `is_lastfm_single()` | ✅ Import (used) | ✅ Used | - |
| `is_musicbrainz_single()` | ✅ Import (used) | ✅ Used | - |
| `secondary_single_lookup()` | ✅ Import (used) | ✅ Used | - |
| `discogs_official_video_signal()` | - | ✅ Used | - |
| `infer_album_context()` | ✅ Import (used) | ✅ Used (10+ places) | - |

### Shared Utilities NOT Safe to Move
| Function | Lines | Used by |
|----------|-------|---------|
| `_canon()` | 7 | `_base_title()`, `_similar()`, rating logic, +15 places |
| `_base_title()` | 6 | `rate_artist()` line 2167 |
| `_has_subtitle_variant()` | 12 | `rate_artist()` line 2168 |
| `_similar()` | 2 | `rate_artist()`, Discogs matching |

---

## popularity.py & mp3scanner.py Analysis

### popularity.py Imports from start.py
```python
from start import (
    get_spotify_artist_id,      # ✅ STAY (API wrapper)
    search_spotify_track,       # ✅ STAY (API wrapper)
    get_lastfm_track_info,      # ✅ STAY (API wrapper)
    get_listenbrainz_score,     # ✅ STAY (API wrapper)
    score_by_age,               # ✅ STAY (scoring logic)
)
```
**Verdict:** All are API wrappers — must remain in start.py

### mp3scanner.py Imports from start.py
```python
from start import get_suggested_mbid  # ✅ STAY (API wrapper around MusicBrainzClient)
```
**Verdict:** API wrapper — must remain in start.py

---

## Dependency Analysis

### Functions ONLY Used by singledetection.py
- None exclusively — all are shared with `rate_artist()`

### Functions Used by BOTH singledetection.py AND rate_artist()
- `is_discogs_single()`
- `is_lastfm_single()`
- `is_musicbrainz_single()`
- `secondary_single_lookup()`
- `infer_album_context()`
- `discogs_official_video_signal()`

### Functions NOT Shared with rating logic
- `_has_official_on_release_top()` — ⚠️ **UNUSED** (can be archived)
- `is_lastfm_single()` — ⚠️ **Placeholder** (returns False, can move)

---

## Safe to Move (Functions with Single Usage)

### ✅ Truly Single-Purpose Functions

| Function | Lines | Risk | Notes |
|----------|-------|------|-------|
| `is_lastfm_single()` | 4 | 🟢 None | Placeholder stub, only in singledetection.py |
| `_has_official_on_release_top()` | 18 | 🟢 None | Unused legacy function |

**Total Saveable (No Risk):** 22 lines

---

## Functions MUST Stay in start.py

| Function | Lines | Why |
|----------|-------|-----|
| `_canon()` | 7 | Core utility for ALL title comparisons (15+ locations) |
| `_base_title()` | 6 | Used by `rate_artist()` for canonical titles |
| `_has_subtitle_variant()` | 12 | Used by `rate_artist()` for title variant detection |
| `_similar()` | 2 | Used by `rate_artist()` + multiple Discogs operations |
| `infer_album_context()` | 16 | Used by BOTH `rate_artist()` AND single detection |
| All API wrappers | ~50 | Required by `popularity.py`, `app.py`, others |
| `enrich_genres_aggressively()` | 50 | Genre enrichment in `rate_artist()` |

---

## Refactoring Options

### Option A: Minimal Extraction (22 lines freed)
**Move to singledetection.py:**
- `is_lastfm_single()` (4 lines)
- `_has_official_on_release_top()` (18 lines)

**Savings:** 22 lines  
**Effort:** 1 hour  
**Risk:** 🟢 None

---

### Option B: Moderate Extraction (~175 lines freed)
**Create new `single_detector.py` module:**
- `secondary_single_lookup()` (53 lines)
- `is_discogs_single()` (8 lines)
- `is_musicbrainz_single()` (4 lines)
- `is_lastfm_single()` (4 lines)
- Video detection helpers (106 lines):
  - `_strip_video_noise()`
  - `_banned_flavor()`
  - `_has_official()`
  - `_release_context_compatible_discogs()`
  - `_release_context_compatible()`
  - `_has_official_on_release_top()`

**Keep in start.py:**
- `discogs_official_video_signal()` (190 lines) — complex, large
- `infer_album_context()` (16 lines) — shared utility
- `_canon()`, title helpers, API wrappers

**Savings:** ~175 lines  
**Effort:** 4-6 hours  
**Risk:** 🟡 Medium (circular imports to manage, but solvable)

---

### Option C: Aggressive Extraction (~450 lines freed)
**Move entire single detection suite to `single_detector.py`:**
- Includes `discogs_official_video_signal()` (190 lines)
- All helpers (~260 lines)
- All single detection functions (~71 lines)

**Savings:** ~450 lines  
**Effort:** 12-16 hours  
**Risk:** 🔴 High (requires refactoring `rate_artist()`, managing global state injection)

---

## Key Findings

### ⚠️ Problem: Multiple Usage Points
Most single detection functions are called from:
1. `singledetection.py` — standalone single detection module
2. `rate_artist()` in `start.py` — main rating function

→ **Cannot move without circular import or refactoring**

### ⚠️ Problem: Large Complex Function
`discogs_official_video_signal()` (190 lines) has 8+ dependencies on helpers that are ONLY used by it.

→ **Could be extracted as a unit to reduce clutter**

### ✅ Solution: Shared Module
Create a `single_detector.py` module that:
- Houses detection logic
- Is imported by both `start.py` (in `rate_artist()`) and `singledetection.py`
- Avoids circular imports by using lazy imports inside functions

---

## Recommendation

**PROCEED WITH OPTION B** — Moderate Extraction

**Rationale:**
- Frees ~175 lines with manageable refactoring effort
- Creates a focused `single_detector.py` module for better code organization
- Keeps shared utilities (`_canon`, title helpers) in `start.py`
- Manages circular import risk with lazy imports in function bodies
- Maintains backward compatibility with external modules (popularity.py, app.py)

**Implementation Steps:**
1. Create `single_detector.py` with single detection functions + helpers
2. Update `start.py` to import from `single_detector` inside `rate_artist()`
3. Update `singledetection.py` to import from both `start.py` and `single_detector.py`
4. Test to ensure no functionality is lost

---

## Estimated Impact

| Metric | Before | After |
|--------|--------|-------|
| start.py line count | 3,114 | ~2,939 |
| Module count | 1 + helpers | 2 focused modules |
| Code organization | Monolithic | Modular |
| Test coverage | To define | Easier to test |

