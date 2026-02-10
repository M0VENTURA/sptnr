# Album Artist Import Clarification

## Issue

PR #250 proposed changing the album_artist import logic from:
```python
"album_artist": t.get("albumArtist", "")
```

to:
```python
album_artist_value = alb.get("artist", artist_name)
"album_artist": album_artist_value
```

However, this change is **incorrect** and should not be implemented.

## Why the Original Code is Correct

### 1. Subsonic API Standard
The Subsonic API (which Navidrome implements) provides an `albumArtist` field on track objects specifically for this purpose. This field contains the album artist metadata from the music file's tags.

### 2. Documented Behavior
From `MISSING_ARTISTS_FIX.md` line 9-11:
> "In `navidrome_import.py` line 333, `album_artist` is populated from Navidrome's `albumArtist` field"

This confirms the intended design.

### 3. The Real Issue Was Already Fixed
The actual problem that users experienced (missing artists in the UI) was NOT caused by using the wrong field during import. It was caused by:
- Navidrome sometimes not providing the `albumArtist` field (resulting in empty strings)
- SQL queries filtering out tracks with empty `album_artist` values

**This was already fixed in PR #249** by using `COALESCE(NULLIF(album_artist, ''), artist)` throughout the app's SQL queries.

## Why PR #250's Approach is Wrong

### 1. Loses Track-Level Specificity
Using `alb.get("artist", artist_name)` applies the SAME album artist value to ALL tracks in an album. This is incorrect for:
- **Compilation albums** (Various Artists) where each track may have a different album artist
- **Split albums** where different tracks/discs have different album artists
- **Featured artist scenarios** where some tracks might have a different album artist than others

### 2. Inconsistent with File Tags
Music file tags contain an `ALBUMARTIST` tag at the track level for a reason - it can vary per track. Overriding this with album-level data loses this granularity.

### 3. Violates Subsonic API Contract
The Subsonic API explicitly provides `albumArtist` at the track level. Using a different field contradicts the API design.

## Correct Approach

**Keep the current implementation**:
```python
"album_artist": t.get("albumArtist", "")
```

**With the SQL fallback pattern from PR #249**:
```sql
COALESCE(NULLIF(album_artist, ''), artist)
```

This combination:
- ✅ Respects track-level album artist metadata from Navidrome
- ✅ Falls back gracefully when album_artist is empty
- ✅ Maintains compatibility with the Subsonic API
- ✅ Supports compilation albums and complex scenarios

## Files Using Correct Pattern

All three import paths correctly use `t.get("albumArtist", "")`:
1. `navidrome_import.py` line 333
2. `start.py` line 599
3. `scan_helpers.py` line 220

## Conclusion

**Do NOT merge PR #250**. The current code is correct. The issue PR #250 was trying to fix doesn't actually exist - it was already properly addressed by PR #249.
