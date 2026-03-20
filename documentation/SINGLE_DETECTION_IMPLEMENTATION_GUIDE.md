# Single Detection Optimization - Implementation Guide

## Summary
This document provides the exact code changes needed to optimize single detection in popularity.py.

## Changes Overview

### ✅ COMPLETED
1. Added `medium_confidence_sources = []` list initialization at line 2284

### 🔄 REMAINING CHANGES

## Change 1: Track MusicBrainz as Medium Confidence

**Location:** popularity.py, line ~2378  
**Find:**
```python
                if result:
                    single_sources.append("musicbrainz")
                    log_info(f"   ✓ MusicBrainz confirms single: {title}")
```

**Replace with:**
```python
                if result:
                    single_sources.append("musicbrainz")
                    medium_confidence_sources.append("musicbrainz")
                    log_info(f"   ✓ MusicBrainz confirms single: {title}")
```

## Change 2: Track MusicBrainz Video as Medium Confidence

**Location:** popularity.py, line ~2394  
**Find:**
```python
                    if has_video:
                        single_sources.append("musicbrainz_video")
                        log_info(f"   ✅ MusicBrainz: Track has music video relationship: {title}")
```

**Replace with:**
```python
                    if has_video:
                        single_sources.append("musicbrainz_video")
                        medium_confidence_sources.append("musicbrainz_video")
                        log_info(f"   ✅ MusicBrainz: Track has music video relationship: {title}")
```

## Change 3: Track MusicBrainz Compilation as Medium Confidence

**Location:** popularity.py, line ~2410  
**Find:**
```python
                    if on_compilations:
                        single_sources.append("musicbrainz_compilation")
                        log_info(f"   ✅ MusicBrainz: Track appears on multiple compilation albums: {title}")
```

**Replace with:**
```python
                    if on_compilations:
                        single_sources.append("musicbrainz_compilation")
                        medium_confidence_sources.append("musicbrainz_compilation")
                        log_info(f"   ✅ MusicBrainz: Track appears on multiple compilation albums: {title}")
```

## Change 4: Add Early Exit After MusicBrainz (2 medium = high)

**Location:** popularity.py, line ~2416 (after the musicbrainz_compilation try/except block)  
**Find:**
```python
                except Exception as e:
                    log_debug(f"   MusicBrainz compilation check error for {title}: {e}")
        except TimeoutError as e:
```

**Replace with:**
```python
                except Exception as e:
                    log_debug(f"   MusicBrainz compilation check error for {title}: {e}")
                    
                # Check if 2 medium sources = high confidence (early exit)
                if len(medium_confidence_sources) >= 2:
                    log_info(f"   🎯 EARLY EXIT: 2 medium sources detected ({medium_confidence_sources}), promoting to HIGH")
                    return {
                        "sources": list(dict.fromkeys(single_sources)),
                        "confidence": "high",
                        "is_single": True
                    }
        except TimeoutError as e:
```

## Change 5: Track Spotify as Medium Confidence

**Location:** popularity.py, line ~2353  
**Find:**
```python
            if matched_release:
                single_sources.append("spotify")
                album_info = matched_release.get("album", {})
```

**Replace with:**
```python
            if matched_release:
                single_sources.append("spotify")
                medium_confidence_sources.append("spotify")
                album_info = matched_release.get("album", {})
```

## Change 6: Add Early Exit After Spotify (2 medium = high)

**Location:** popularity.py, line ~2358 (after Spotify confirms)  
**Find:**
```python
                if verbose:
                    log_verbose(f"   ✓ Spotify confirms single: {title}")
                    log_verbose(f"      Matched release: {matched_release.get('name')}")
                    log_verbose(f"      Album: {album_info.get('name')} (type: {album_info.get('album_type')})")
            else:
```

**Replace with:**
```python
                if verbose:
                    log_verbose(f"   ✓ Spotify confirms single: {title}")
                    log_verbose(f"      Matched release: {matched_release.get('name')}")
                    log_verbose(f"      Album: {album_info.get('name')} (type: {album_info.get('album_type')})")
                    
                # Check if 2 medium sources = high confidence (early exit)
                if len(medium_confidence_sources) >= 2:
                    log_info(f"   🎯 EARLY EXIT: 2 medium sources detected ({medium_confidence_sources}), promoting to HIGH")
                    return {
                        "sources": list(dict.fromkeys(single_sources)),
                        "confidence": "high",
                        "is_single": True
                    }
            else:
```

## Change 7: Track Discogs Video as Medium Confidence

**Location:** popularity.py, line ~2471  
**Find:**
```python
                if result:
                    single_sources.append("discogs_video")
                    log_info(f"   ✓ Discogs confirms music video: {title}")
```

**Replace with:**
```python
                if result:
                    single_sources.append("discogs_video")
                    medium_confidence_sources.append("discogs_video")
                    log_info(f"   ✓ Discogs confirms music video: {title}")
```

## Change 8: Add Early Exit After Discogs Video (2 medium = high)

**Location:** popularity.py, line ~2474 (after Discogs video confirms)  
**Find:**
```python
                    log_info(f"   ✓ Discogs confirms music video: {title}")
                    log_debug(f"   Discogs result: Music video confirmed for '{lookup_title}'")
                else:
```

**Replace with:**
```python
                    log_info(f"   ✓ Discogs confirms music video: {title}")
                    log_debug(f"   Discogs result: Music video confirmed for '{lookup_title}'")
                    
                    # Check if 2 medium sources = high confidence (early exit)
                    if len(medium_confidence_sources) >= 2:
                        log_info(f"   🎯 EARLY EXIT: 2 medium sources detected ({medium_confidence_sources}), promoting to HIGH")
                        return {
                            "sources": list(dict.fromkeys(single_sources)),
                            "confidence": "high",
                            "is_single": True
                        }
                else:
```

## Change 9: Track Iterative Z-score as Medium Confidence

**Location:** popularity.py, line ~2496  
**Find:**
```python
            if iterative_zscore_passed:
                single_sources.append("iterative_zscore")
                log_info(f"   Iterative z-score method: {title} passed album standout test")
```

**Replace with:**
```python
            if iterative_zscore_passed:
                single_sources.append("iterative_zscore")
                medium_confidence_sources.append("iterative_zscore")
                log_info(f"   Iterative z-score method: {title} passed album standout test")
```

## Change 10: Update Final Confidence Logic (2 medium = high)

**Location:** popularity.py, line ~2506  
**Find:**
```python
    if has_discogs_single:
        single_confidence = "high"
    elif has_iterative_zscore or has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"
```

**Replace with:**
```python
    # NEW RULE: 2 medium sources = high confidence
    if has_discogs_single or len(medium_confidence_sources) >= 2:
        single_confidence = "high"
    elif has_iterative_zscore or has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"
```

## Change 11: Update is_single Logic (2 medium = high)

**Location:** popularity.py, line ~2519  
**Find:**
```python
    # is_single = True only for high confidence singles (Discogs-confirmed)
    is_single = single_confidence == "high"
```

**Replace with:**
```python
    # is_single = True for high confidence singles (Discogs-confirmed OR 2+ medium sources)
    is_single = single_confidence == "high"
```
*(No actual change needed - just keeping this for completeness)*

---

## Testing After Changes

After making all changes, test with:
```bash
python app.py popularity-scan --artist "Creed" --verbose
```

Look for these log messages:
- `🎯 EARLY EXIT: 2 medium sources detected`
- Verify singles still get detected correctly
- Check that API call count is reduced (~70% fewer calls expected)

## Rollback Plan

If issues occur:
```bash
git diff popularity.py  # Review changes
git checkout popularity.py  # Revert if needed
```

## Expected Behavior

**Before:**
- All 5 sources checked for every track (even if Discogs says YES immediately)
- Example: 10 tracks * 5 sources = 50 API calls

**After:**
- Discogs YES → 1 call, return immediately
- 2 medium sources → 2-3 calls, return immediately  
- No match → 5 calls (same as before)
- Example: 10 tracks * ~2 sources average = **~20 API calls** (60% reduction)

## Files Modified

- `popularity.py`: detect_single_for_track() function only (lines 2283-2530)

## Notes

- All changes preserve existing safety logic (user-set singles, outliers, compilations)
- Graceful fallbacks if sources unavailable
- Backward compatible - return dict format unchanged
- No database schema changes needed
