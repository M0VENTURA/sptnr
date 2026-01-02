# Quick Reference: Function Analysis Matrix

## Master Function Table

| Function | Lines | Location | Type | singledetection.py | rate_artist() | popularity.py | mp3scanner.py | Can Move? | Risk |
|----------|-------|----------|------|-------------------|----------------|---------------|---------------|-----------|------|
| **SINGLE DETECTION** |
| `is_discogs_single()` | 8 | 1117-1124 | Wrapper | ✅ Imported | ✅ Used (L2176) | - | - | ✅ YES | 🟡 MED |
| `is_lastfm_single()` | 4 | 1127-1130 | Stub | ✅ Imported | ✅ Used (L2277) | - | - | ✅ YES | 🟢 NONE |
| `is_musicbrainz_single()` | 4 | 1131-1134 | Wrapper | ✅ Imported | ✅ Used (L2193) | - | - | ✅ YES | 🟡 MED |
| `secondary_single_lookup()` | 53 | 1136-1188 | Aggregator | ✅ Imported | ✅ Used (L2338) | - | - | ✅ YES | 🟡 MED |
| **VIDEO DETECTION** |
| `discogs_official_video_signal()` | 190 | 926-1115 | Complex | - | ✅ Used (L2212) | - | - | ⚠️ MAYBE | 🟡 MED |
| `infer_album_context()` | 16 | 893-908 | Helper | ✅ Imported | ✅ Used (10+ places) | - | - | ❌ NO | 🔴 HIGH |
| **VIDEO HELPERS** |
| `_strip_video_noise()` | 18 | 863-880 | Helper | - | - | - | - | ✅ YES | 🟢 NONE |
| `_banned_flavor()` | 16 | 909-924 | Helper | - | - | - | - | ✅ YES | 🟢 NONE |
| `_has_official()` | 11 | 881-891 | Helper | - | - | - | - | ✅ YES | 🟢 NONE |
| `_has_official_on_release_top()` | 18 | 838-855 | Helper | - | - | - | - | ✅ YES | 🟢 NONE |
| `_release_context_compatible()` | 6 | 831-836 | Wrapper | - | - | - | - | ✅ YES | 🟢 NONE |
| `_release_context_compatible_discogs()` | 18 | 812-829 | Helper | - | - | - | - | ✅ YES | 🟢 NONE |
| **CORE UTILITIES (MUST STAY)** |
| `_canon()` | 7 | 694-700 | Core | - | ✅ Many | - | - | ❌ NO | 🔴 HIGH |
| `_base_title()` | 6 | 704-709 | Helper | - | ✅ L2167 | - | - | ❌ NO | 🔴 HIGH |
| `_has_subtitle_variant()` | 12 | 711-722 | Helper | - | ✅ L2168 | - | - | ❌ NO | 🔴 HIGH |
| `_similar()` | 2 | 724-725 | Wrapper | - | ✅ L2169 | - | - | ❌ NO | 🔴 HIGH |
| **API WRAPPERS (MUST STAY)** |
| `get_spotify_artist_id()` | 3 | ~1301 | Wrapper | - | - | ✅ Used | - | ❌ NO | 🔴 HIGH |
| `get_spotify_artist_single_track_ids()` | 3 | ~1310 | Wrapper | - | - | - | - | ❌ NO | 🔴 HIGH |
| `search_spotify_track()` | 3 | ~1319 | Wrapper | - | - | ✅ Used | - | ❌ NO | 🔴 HIGH |
| `get_lastfm_track_info()` | 3 | ~1391 | Wrapper | - | - | ✅ Used | - | ❌ NO | 🔴 HIGH |
| `get_listenbrainz_score()` | 3 | ~1398 | Wrapper | - | - | ✅ Used | - | ❌ NO | 🔴 HIGH |
| `get_suggested_mbid()` | 3 | 1197-1199 | Wrapper | - | - | - | ✅ Used | ❌ NO | 🔴 HIGH |
| `get_discogs_genres()` | 2 | ~1407 | Wrapper | - | - | - | - | ❌ NO | 🔴 HIGH |
| `get_audiodb_genres()` | 2 | ~1413 | Wrapper | - | - | - | - | ❌ NO | 🔴 HIGH |
| `get_musicbrainz_genres()` | 3 | ~1419 | Wrapper | - | - | - | - | ❌ NO | 🔴 HIGH |
| `score_by_age()` | 2 | ~1424 | Wrapper | - | - | ✅ Used | - | ❌ NO | 🔴 HIGH |
| **DISCOGS SESSION MGMT** |
| `_get_discogs_session()` | 10 | 755-764 | Factory | - | ❓ Maybe | - | - | ⚠️ MAYBE | 🟡 MED |
| `_throttle_discogs()` | 9 | 771-779 | Limiter | - | ❓ Maybe | - | - | ⚠️ MAYBE | 🟡 MED |
| `_respect_retry_after()` | 9 | 782-790 | Handler | - | ❓ Maybe | - | - | ⚠️ MAYBE | 🟡 MED |
| `_discogs_search()` | 16 | 794-809 | Search | - | ❓ Maybe | - | - | ⚠️ MAYBE | 🟡 MED |
| **OTHER CRITICAL** |
| `enrich_genres_aggressively()` | 50+ | 3051-3100 | Enricher | - | ✅ L1869 | - | - | ❌ NO | 🔴 HIGH |

---

## Summary by Category

### ✅ Safe to Move Immediately (Total: 91 lines)
```
_has_official_on_release_top()          18 lines
_release_context_compatible_discogs()   18 lines
_banned_flavor()                        16 lines
_strip_video_noise()                    18 lines
_has_official()                         11 lines
_release_context_compatible()           6 lines
is_lastfm_single()                      4 lines
───────────────────────────────────────────────
SUBTOTAL:                               91 lines
```

### ⚠️ Can Move With Refactoring (Total: 255 lines)
```
discogs_official_video_signal()         190 lines
secondary_single_lookup()               53 lines
is_discogs_single()                     8 lines
is_musicbrainz_single()                 4 lines
───────────────────────────────────────────────
SUBTOTAL:                               255 lines
```

### ❌ Must Stay (Total: 93+ lines)
```
_canon()                                7 lines
_base_title()                           6 lines
_has_subtitle_variant()                 12 lines
_similar()                              2 lines
infer_album_context()                   16 lines
All API wrappers (get_spotify_*, etc)   ~50 lines
───────────────────────────────────────────────
SUBTOTAL:                               93+ lines
```

---

## Extraction Plans

### Plan A: Minimal (Quick Win)
**Lines Freed:** 91  
**Effort:** 1 hour  
**Risk:** 🟢 None

Move to `singledetection.py`:
- `_has_official_on_release_top()`
- `is_lastfm_single()`
- Other 5 video helpers (unused or video-only)

---

### Plan B: Moderate (Recommended)
**Lines Freed:** 346  
**Effort:** 4-6 hours  
**Risk:** 🟡 Medium

Create new `single_detector.py` with:
- All single detection functions (65 lines)
- All video detection helpers (106 lines)
- `discogs_official_video_signal()` (190 lines)

Keep in `start.py`:
- Shared utilities (`_canon()`, title helpers)
- `infer_album_context()` (import if needed)
- All API wrappers (external dependencies)

---

### Plan C: Aggressive (Comprehensive)
**Lines Freed:** 346+  
**Effort:** 12-16 hours  
**Risk:** 🔴 High

Same as Plan B but also:
- Refactor `rate_artist()` to inject detection functions
- Move helper functions to separate `title_helpers.py`
- Requires careful testing of all code paths

---

## External Dependencies (Do NOT Move)

### popularity.py requires:
- `get_spotify_artist_id()`
- `search_spotify_track()`
- `get_lastfm_track_info()`
- `get_listenbrainz_score()`
- `score_by_age()`

### mp3scanner.py requires:
- `get_suggested_mbid()`

### app.py requires:
- `create_retry_session()` (from helpers)
- `spotify_client` (global)
- `get_suggested_mbid()`
- `_discogs_search()`
- `_get_discogs_session()`

---

## Circular Import Prevention

**Problem:** If `single_detector.py` is created:
```
start.py imports single_detector.py  (for discogs_official_video_signal)
         ↓
single_detector.py needs config, CONTEXT_GATE, global clients
         ↓
Must import from start.py → CIRCULAR
```

**Solution:** Lazy imports within function bodies
```python
# In single_detector.py
def secondary_single_lookup(...):
    # Import only when needed
    from start import discogs_official_video_signal, CONTEXT_FALLBACK_STUDIO
    # ... rest of function
```

---

## Line Count Impact

| Component | Current | After Plan B | Difference |
|-----------|---------|--------------|-----------|
| start.py | 3,114 | ~2,768 | -346 lines (-11%) |
| single_detector.py | - | 346 | +346 lines (NEW) |
| singledetection.py | 170 | 170 | 0 (no change) |
| **Total Module Size** | 3,284 | 3,284 | 0 (same) |

**Benefit:** Better organization, focused modules, clearer dependencies

---

## Recommendation

**Implement Plan B (Moderate Extraction)**

**Rationale:**
- ✅ Frees meaningful amount of code (346 lines, 11% of file)
- ✅ Creates focused, testable module
- ✅ Manages complexity without massive refactoring
- ✅ Preserves all functionality
- ⚠️ Requires careful dependency injection
- ⚠️ Need to test circular import handling

**Steps:**
1. Create `single_detector.py` (346 lines)
2. Move functions & helpers
3. Use lazy imports to prevent circular deps
4. Update `rate_artist()` to import from `single_detector`
5. Test thoroughly with both `rate_artist()` and `singledetection.py`
6. Update documentation

**Effort:** 4-6 developer hours  
**Testing:** 2-3 hours  
**Risk Level:** 🟡 Medium (manageable with good QA)
