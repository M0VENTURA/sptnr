# Popularity and Z-Score Filtering Implementation

## Problem Statement

Tracks at the end of albums with parenthetical suffixes (e.g., "Track Title (Single)", "Track Title (Live in Wacken 2022)") were throwing off popularity statistics and z-score calculations. These bonus/live/alternate tracks have significantly lower or higher popularity scores than regular album tracks, which affected the mean and caused incorrect HIGH CONFIDENCE ratings.

### Example from User Logs

For the album "Feuerschwanz - Fegefeuer":
- Regular tracks: 67.5, 67.0, 66.5, 64.5, 64.5, 62.5, 43.5
- Live tracks: 12.0, 10.0, 9.0, 9.0, 9.0, 9.0, 8.5, 8.5, 8.5, 8.0, 7.5, 7.0, 6.0
- Single versions: "Uruk-Hai (Single)" at 67.5, "SGFRD Dragonslayer (Single)" at 67.0, etc.

Without filtering:
- Mean: 27.40
- High Confidence Threshold: 33.40
- Result: 7 tracks incorrectly marked as HIGH CONFIDENCE (including "Bastard von Asgard" at 43.5)

## Solution

Added a filtering function `should_exclude_from_stats()` that:

1. **Identifies tracks ending with parenthesized suffixes** using regex pattern `^.*\([^)]*\)$`
2. **Only excludes consecutive tracks at the END of the album** (sorted by popularity descending)
3. **Requires at least 2 consecutive tracks** to avoid false positives
4. **Doesn't filter small albums** (< 3 tracks)

### Key Implementation Details

- Tracks are ordered by popularity (descending), so low-popularity bonus tracks appear at the end
- Pattern matches only titles that END with a parenthesized suffix (e.g., "Title (Single)", "Title (Live)")
- Does NOT match titles with parentheses in the middle (e.g., "Track (One) Name")
- Uses set membership for O(1) lookup performance
- All tracks are still rated, but statistics (mean/stddev) use only filtered scores
- Works backwards from the last track to find consecutive tracks with suffix

## Impact

With filtering enabled:
- Mean: 62.29 (up from 27.40, +127%)
- High Confidence Threshold: 68.29 (up from 33.40)
- Result: More accurate ratings, no false HIGH CONFIDENCE tracks

### Before and After Comparison

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Mean Popularity | 27.40 | 62.29 | +34.89 (+127%) |
| High Confidence Threshold | 33.40 | 68.29 | +34.89 |
| HIGH CONFIDENCE Tracks | 7 | 0-6* | More accurate |

*Depends on actual track popularity vs. new threshold

## Files Modified

1. **popularity.py**
   - Updated `should_exclude_from_stats()` function to use pattern `^.*\([^)]*\)$`
   - Modified statistics calculation to filter excluded tracks (lines 1437-1444)
   - Added `import re` at module level (line 20)

2. **Test Files Updated**
   - `test_parenthesis_filter.py` - Unit tests for filtering logic
   - `test_integration_popularity_filtering.py` - Integration test with real-world scenario

## Testing

All tests pass:

✅ **Basic exclusion** - Correctly excludes 13 consecutive tracks ending with "(Live in Wacken 2022)"
✅ **Single track** - No exclusion for single track with parentheses
✅ **Non-consecutive** - No exclusion for non-consecutive tracks with parentheses
✅ **Parentheses in middle** - No exclusion for titles with parentheses in the middle (not at end)
✅ **Consecutive at end** - Correctly excludes only consecutive tracks at end
✅ **Small albums** - No filtering for albums with < 3 tracks
✅ **Single suffix** - Correctly excludes tracks ending with "(Single)"
✅ **Integration** - Mean increases from 27.40 to 62.29 as expected

## Performance

- Set membership testing: O(1)
- Overall complexity: O(n) for iteration
- Minimal overhead: Only processes tracks once during statistics calculation

## Edge Cases Handled

1. **Non-consecutive tracks with parentheses** - Not excluded (e.g., "(Intro)" at start, "(Outro)" at end)
2. **Single track with parentheses** - Not excluded (requires 2+ consecutive)
3. **Small albums** - Not filtered (< 3 tracks)
4. **Tracks with parentheses in middle** - Not excluded (pattern only matches suffix at end)
5. **Tracks with parentheses in middle of title** - Not excluded (e.g., "Track (One) Name")

## Future Considerations

This implementation could be extended to:
- Filter tracks based on specific keywords (e.g., only "Live", "Bonus")
- Make the minimum consecutive count configurable
- Add configuration option to enable/disable filtering
- Log which tracks were excluded for debugging

## Security

✅ No security vulnerabilities detected by CodeQL scanner
