# Discogs Single Detection Verification

## Overview

This implementation provides comprehensive Discogs metadata fetching and single detection verification for the SPTNR music library management system.

## Features Implemented

### 1. Discogs API Access
- ✅ Authenticated requests with User-Agent and token
- ✅ Rate limiting (configurable, default ~171 req/min max)
- ✅ Retry logic for 500 errors with exponential backoff
- ✅ Proper handling of 429 (rate limit) responses

### 2. Release Lookup
- ✅ GET /releases/{id} endpoint
- ✅ Response includes all required fields:
  - formats[]
  - format descriptions
  - tracklist[]
  - title
  - artists[]
  - master_id (if present)

### 3. Format Parsing
- ✅ Checks formats[].name for: "Single", "7\"", "12\" Single", "CD Single", "Promo"
- ✅ Checks formats[].descriptions for: "Single", "Maxi-Single"
- ✅ Correctly excludes "EP" from single detection
- ✅ Handles Promo releases

### 4. Master Release Handling
- ✅ GET /masters/{master_id} endpoint
- ✅ Master release formats checked for "Single"
- ✅ Master tracklist used for title matching

### 5. Track Matching
- ✅ Title normalization:
  - Case-insensitive
  - Punctuation removed
  - Bracketed suffixes removed (e.g., "(Remix)")
  - Dash-based suffixes handled
- ✅ Duration matching with ±2 second tolerance
- ✅ Alternate version filtering (live, remix, acoustic, demo, etc.)

### 6. Single Determination Rules
A release is considered a single if ANY of the following are true:
- ✅ formats[].name contains "Single"
- ✅ formats[].descriptions contains "Single" or "Maxi-Single"
- ✅ Release has 1-2 tracks
- ✅ Release is a promo with 1-2 tracks
- ✅ Master release is tagged as a single

### 7. Error Handling
- ✅ 500 errors trigger retry with exponential backoff
- ✅ Missing fields handled gracefully (no crashes)
- ✅ Fallback logic for incomplete data
- ✅ Disabled client returns safe defaults

### 8. Database Storage
All Discogs fields are stored in the database:
- ✅ discogs_release_id
- ✅ discogs_master_id
- ✅ discogs_formats (JSON array)
- ✅ discogs_format_descriptions (JSON array)
- ✅ discogs_is_single (boolean)
- ✅ discogs_track_titles (JSON array)
- ✅ discogs_release_year
- ✅ discogs_label
- ✅ discogs_country

### 9. Cross-Source Validation
- ✅ Discogs integrated with existing single detection in popularity.py
- ✅ Works alongside Spotify and MusicBrainz
- ✅ Discogs is additive (doesn't override other sources)

## Files

### Core Implementation
- `discogs_verification.py` - Comprehensive verification client with all rules
- `api_clients/discogs.py` - Enhanced with metadata storage support
- `migrations/add_discogs_metadata_columns.sql` - Database migration
- `check_db.py` - Updated schema definition

### Tests
- `test_discogs_verification.py` - Comprehensive test suite (9 tests, all passing)
- `test_discogs_integration.py` - End-to-end integration test

## Test Results

### Verification Test Suite
```
✅ PASS: 1. API Access
✅ PASS: 2. Release Lookup
✅ PASS: 3. Format Parsing
✅ PASS: 4. Master Release Handling
✅ PASS: 5. Track Matching
✅ PASS: 6. Single Determination Rules
✅ PASS: 7. Error Handling
✅ PASS: 8. Database Storage
✅ PASS: 9. Cross-Source Validation

✅ ALL TESTS PASSED
```

### Integration Test
```
✅ INTEGRATION TEST PASSED
Discogs metadata can be successfully stored and retrieved!
```

## Usage

### Using DiscogsVerificationClient

```python
from discogs_verification import DiscogsVerificationClient

client = DiscogsVerificationClient(token="your_discogs_token", enabled=True)

# Determine single status
result = client.determine_single_status(
    track_title="Song Title",
    artist="Artist Name",
    track_duration=225.0  # Optional, in seconds
)

print(f"Is single: {result['is_single']}")
print(f"Confidence: {result['confidence']}")
print(f"Source: {result['source']}")
```

### Using Enhanced DiscogsClient

```python
from api_clients.discogs import DiscogsClient

client = DiscogsClient(token="your_discogs_token", enabled=True)

# Get comprehensive metadata for database storage
metadata = client.get_comprehensive_metadata(
    title="Song Title",
    artist="Artist Name",
    duration=225.0  # Optional
)

if metadata:
    print(f"Release ID: {metadata['discogs_release_id']}")
    print(f"Is single: {metadata['discogs_is_single']}")
    print(f"Formats: {metadata['discogs_formats']}")
```

### Database Storage

```python
import json
import sqlite3

# Store metadata
cursor.execute("""
    UPDATE tracks SET
        discogs_release_id = ?,
        discogs_master_id = ?,
        discogs_formats = ?,
        discogs_format_descriptions = ?,
        discogs_is_single = ?,
        discogs_track_titles = ?,
        discogs_release_year = ?,
        discogs_label = ?,
        discogs_country = ?
    WHERE id = ?
""", (
    metadata['discogs_release_id'],
    metadata['discogs_master_id'],
    json.dumps(metadata['discogs_formats']),
    json.dumps(metadata['discogs_format_descriptions']),
    1 if metadata['discogs_is_single'] else 0,
    json.dumps(metadata['discogs_track_titles']),
    metadata['discogs_release_year'],
    metadata['discogs_label'],
    metadata['discogs_country'],
    track_id
))
```

## Configuration

The Discogs token should be configured in `config.yaml`:

```yaml
api_integrations:
  discogs:
    enabled: true
    token: "your_discogs_api_token"
```

## Rate Limiting

- Default: 1 request per 0.35 seconds (~171 requests/minute)
- Respects Discogs rate limit headers (429 responses)
- Automatic retry with exponential backoff on 500 errors
- Configurable via `_DISCOGS_MIN_INTERVAL` in code

## Testing

Run the verification test suite:
```bash
python3 test_discogs_verification.py
```

Run the integration test:
```bash
python3 test_discogs_integration.py
```

## Migration

To add Discogs columns to an existing database:
```bash
sqlite3 sptnr.db < migrations/add_discogs_metadata_columns.sql
```

Or use the automatic schema update:
```python
import check_db
check_db.update_schema('sptnr.db')
```

## Compliance with Requirements

This implementation fully satisfies all 9 requirements from the problem statement:

1. ✅ Discogs API Access - Complete with auth, rate limiting, retry logic
2. ✅ Release Lookup - GET /releases/{id} with all fields
3. ✅ Format Parsing - All format types and descriptions
4. ✅ Master Release Handling - GET /masters/{id} with format checking
5. ✅ Track Matching - Title normalization, duration matching, alternate filtering
6. ✅ Single Determination Rules - All 5 rules implemented
7. ✅ Error Handling - Retries, graceful degradation, fallback logic
8. ✅ Database Storage - All 9 Discogs fields stored
9. ✅ Cross-Source Validation - Integrated with Spotify/MusicBrainz

## Future Enhancements

Potential improvements for future versions:
- Cache Discogs API responses to reduce API calls
- Batch processing for multiple tracks
- Advanced fuzzy matching for track titles
- Video detection integration (already implemented in DiscogsClient)
- Genre metadata extraction
- Album art URL extraction
