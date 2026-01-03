# Analysis Complete: start.py Refactoring Study

## Executive Summary

**Analysis of:** `c:\Script\Github\sptnr-1\start.py` (3,114 lines)

**Objective:** Identify functions safe to move to reduce file size and improve modularity

**Status:** ✅ COMPLETE

---

## Key Findings

### Functions That CAN Move (346 lines total)

#### Safe Single Detection Suite (260 lines):
- `is_discogs_single()` — 8 lines
- `is_lastfm_single()` — 4 lines  
- `is_musicbrainz_single()` — 4 lines
- `secondary_single_lookup()` — 53 lines
- `discogs_official_video_signal()` — 190 lines

#### Safe Helper Functions (90 lines):
- `_strip_video_noise()` — 18 lines
- `_banned_flavor()` — 16 lines
- `_has_official()` — 11 lines
- `_release_context_compatible_discogs()` — 18 lines
- `_release_context_compatible()` — 6 lines
- `_has_official_on_release_top()` — 18 lines (unused)

**Recommended Destination:** New `single_detector.py` module

---

### Functions That MUST Stay (93+ lines)

#### Core Utilities:
- `_canon()` — 7 lines (used 15+ places)
- `_base_title()` — 6 lines (used by rate_artist)
- `_has_subtitle_variant()` — 12 lines (used by rate_artist)
- `_similar()` — 2 lines (used by rate_artist)
- `infer_album_context()` — 16 lines (used by both detection AND rating)

#### API Wrappers (required by external modules):
- `get_spotify_artist_id()` (used by popularity.py)
- `search_spotify_track()` (used by popularity.py)
- `get_lastfm_track_info()` (used by popularity.py)
- `get_listenbrainz_score()` (used by popularity.py)
- `get_suggested_mbid()` (used by mp3scanner.py)
- Plus 15+ other API wrapper functions

#### Other Critical:
- `enrich_genres_aggressively()` — 50+ lines (used by rate_artist)

---

## Usage Analysis Results

### in singledetection.py:
```
is_discogs_single           ✅ Imported (lines 27, 78)
is_lastfm_single            ✅ Imported (lines 28, 91)
is_musicbrainz_single       ✅ Imported (lines 29, 78)
secondary_single_lookup     ✅ Imported (lines 30, 117)
infer_album_context         ✅ Imported (lines 27, throughout)
discogs_official_video_signal   ✅ Would need to import
```

### in rate_artist():
```
is_discogs_single           ✅ Used (line 2176)
is_lastfm_single            ✅ Used (line 2277)
is_musicbrainz_single       ✅ Used (line 2193)
secondary_single_lookup     ✅ Used (line 2338)
discogs_official_video_signal   ✅ Used (line 2212)
infer_album_context         ✅ Used (10+ places)
_base_title                 ✅ Used (line 2167)
_has_subtitle_variant       ✅ Used (line 2168)
_similar                    ✅ Used (line 2169)
```

### in popularity.py:
```
get_spotify_artist_id       ✅ REQUIRED (line 32)
search_spotify_track        ✅ REQUIRED (line 33)
get_lastfm_track_info       ✅ REQUIRED (line 34)
get_listenbrainz_score      ✅ REQUIRED (line 35)
score_by_age                ✅ REQUIRED (line 36)
```

### in mp3scanner.py:
```
get_suggested_mbid          ✅ REQUIRED (line 319)
```

---

## Recommended Action: Plan B (Moderate Extraction)

### What Moves:
**Create new `single_detector.py` file (346 lines)**
- All single detection functions
- All video detection helpers
- `discogs_official_video_signal()` (complex video detection)

### What Stays in start.py:
- Core utilities: `_canon()`, `_base_title()`, `_has_subtitle_variant()`, `_similar()`
- Shared context: `infer_album_context()`
- All API wrappers (for external module compatibility)
- All rating-specific code

### Impact:
| Metric | Before | After | Change |
|--------|--------|-------|--------|
| start.py lines | 3,114 | ~2,768 | -346 (-11%) |
| Module count | 1 main | 2 focused | Better organization |
| Code reusability | Mixed | Improved | Clearer separation |

### Effort & Risk:
| Aspect | Details |
|--------|---------|
| Implementation time | 4-6 hours |
| Testing time | 2-3 hours |
| Risk level | 🟡 Medium (manageable) |
| Complexity | Moderate (lazy imports needed) |

---

## Dependencies Overview

### Single Detection Dependencies (from other functions in start.py):
```
secondary_single_lookup()
├── is_discogs_single()
├── discogs_official_video_signal()
│   ├── _get_discogs_session()
│   ├── _throttle_discogs()
│   ├── _respect_retry_after()
│   ├── _strip_video_noise()
│   │   └── _canon()
│   ├── _banned_flavor()
│   ├── _release_context_compatible_discogs()
│   └── [nested: _inspect_release()]
├── is_musicbrainz_single()
├── is_lastfm_single()
└── [Global: DISCOGS_TOKEN, CONTEXT_GATE, config]
```

### Shared Dependencies:
- `infer_album_context()` — Used by BOTH rate_artist() AND single_detector
- `_canon()` — Core utility, used throughout
- Global clients: `discogs_client`, `musicbrainz_client`

---

## Documentation Generated

Created 5 comprehensive analysis documents:

1. **ANALYSIS_SUMMARY.md** — High-level overview with key findings
2. **DETAILED_ANALYSIS.md** — Complete function-by-function breakdown  
3. **FUNCTION_CALL_ANALYSIS.md** — Call chains and dependencies
4. **QUICK_REFERENCE.md** — Matrix tables for quick lookup
5. **IMPLEMENTATION_GUIDE.md** — Step-by-step extraction instructions

All files located in: `c:\Script\Github\sptnr-1\`

---

## Next Steps

If you want to proceed with Plan B (recommended):

1. **Review** `IMPLEMENTATION_GUIDE.md` for detailed steps
2. **Create** new `single_detector.py` file
3. **Copy** 346 lines of code to new module
4. **Update** imports in `start.py`, `singledetection.py`
5. **Test** thoroughly with all modules
6. **Validate** no functionality is broken

---

## Summary Table

| Function | Lines | Current Usage | Can Move? | Risk | Priority |
|----------|-------|----------------|-----------|------|----------|
| `secondary_single_lookup()` | 53 | rate_artist + singledetection | ✅ YES | 🟡 MED | HIGH |
| `discogs_official_video_signal()` | 190 | rate_artist + secondary | ✅ YES | 🟡 MED | HIGH |
| `is_discogs_single()` | 8 | rate_artist + singledetection | ✅ YES | 🟡 MED | HIGH |
| `is_musicbrainz_single()` | 4 | rate_artist + singledetection | ✅ YES | 🟡 MED | HIGH |
| `is_lastfm_single()` | 4 | rate_artist + singledetection | ✅ YES | 🟢 NONE | MEDIUM |
| `_strip_video_noise()` | 18 | video detection only | ✅ YES | 🟢 NONE | LOW |
| `_banned_flavor()` | 16 | video detection only | ✅ YES | 🟢 NONE | LOW |
| `_has_official()` | 11 | video detection only | ✅ YES | 🟢 NONE | LOW |
| `_has_official_on_release_top()` | 18 | UNUSED (legacy) | ✅ YES | 🟢 NONE | ARCHIVE |
| `_release_context_compatible_discogs()` | 18 | video detection only | ✅ YES | 🟢 NONE | LOW |
| `_release_context_compatible()` | 6 | video detection only | ✅ YES | 🟢 NONE | LOW |
| **TOTAL** | **346** | | | | |

---

## Circular Import Prevention

When `single_detector.py` is created, use **lazy imports** inside function bodies:

```python
# In single_detector.py
def secondary_single_lookup(...):
    # Import only when function is called, not at module load time
    from start import DISCOGS_TOKEN, config, discogs_official_video_signal
    # ... rest of implementation
```

This avoids:
```
start.py → imports single_detector.py
single_detector.py → imports from start.py  ❌ CIRCULAR
```

By using lazy imports inside functions, the circular dependency is broken.

---

## Conclusion

**All 346 moveable lines are well-isolated and ready for extraction.**

- ✅ No hidden dependencies
- ✅ Clear call patterns
- ✅ External modules (popularity.py, mp3scanner.py) unaffected
- ✅ rating logic (rate_artist) can lazily import

**Ready for implementation when you are.**

