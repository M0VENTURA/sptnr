# Wikipedia Scraper Fixes

## Issues Fixed

### 1. Genre Columns Being Parsed as Artist Names

**Problem**: Wikipedia tables for music releases contain genre/style columns that were being parsed as artist names, resulting in malformed data like:

- Artist: "Stoner metalSouthern metalsludge metalhardcore punk"
- Album: "Corrosion of Conformity" (the actual artist)
- Date: "2026-04-01"

**Root Cause**: The scraper's `_parse_row_for_month()` method didn't account for genre columns appearing in some Wikipedia tables. Different Wikipedia sources have different table structures:

- Some tables: [Citation] | [Genres] | [Day] | [Artist] | [Album]
- Others: [Day] | [Artist] | [Album]

**Solution**:

1. Added `_is_genre_column()` method to detect if a cell contains genre information
   - Looks for comma-separated values with genre keywords (metal, rock, pop, etc.)
   - Identifies multi-part genre fields

2. Updated `_parse_row_for_month()` to:
   - Skip first column if it's empty or contains genre info
   - Filter out 'genre' entries from column orders
   - Validate that parsed artist/album don't look like genre data
   - Added extra validation to prevent genre-like strings from being saved

3. Updated `SOURCE_COLUMN_ORDERS` to mark genre columns explicitly

## Impact

- Wikipedia scraper now correctly parses releases regardless of table structure
- Eliminates malformed artist/album names in upcoming_releases table
- Improves data quality for release matching

## Testing

To test the fix:

1. **Clear old data**:

   ```bash
   curl -X POST http://localhost:5000/api/upcoming-releases/clear
   ```

2. **Re-scrape Wikipedia**:

   ```bash
   curl -X POST http://localhost:5000/api/upcoming-releases/scrape
   ```

3. **Verify data quality** - Check the Upcoming Releases page for properly formatted artist/album names

## Files Modified

- `wikipedia_releases_scraper.py` - Added genre detection and improved parsing logic

## Related Issues

Still investigating:

- 404 error on `genre-utils.js` - May be a static file serving issue in production
- 500 errors on `/api/track/` and `/api/tags/track/` endpoints
- 400 error on `/api/queue/add` endpoint

These appear to be unrelated to the Wikipedia scraper and may require checking server logs for details.
