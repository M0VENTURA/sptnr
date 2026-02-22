# Implementation Summary: Soulseek Queue Retry & Wikipedia MusicBrainz Search

## Overview
This PR implements two major features requested in the GitHub issue:

1. **Unlimited Soulseek Queue Retries**: Failed searches now retry indefinitely every 30 minutes instead of stopping after 5 attempts
2. **Wikipedia MusicBrainz Search Integration**: Changed Wikipedia download button to search MusicBrainz and display full track listings before adding to queue

## Changes Made

### Part 1: Soulseek Queue Retry Changes

#### Modified Files
- `queue_processor.py`

#### Key Changes
1. **`mark_failed()` function** (lines 168-207):
   - Removed the `max_retries` check that caused items to permanently fail after 5 attempts
   - Now always schedules retry when `schedule_retry=True`, regardless of retry count
   - Changed logging to show retry number without max comparison: `"scheduling retry #{retry_count}"`
   - Returns `schedule_retry` boolean to indicate whether retry was scheduled

2. **`increment_retry_count()` function** (lines 131-165):
   - Removed `max_retries` field from SQL query
   - Simplified to only increment retry count and schedule next retry
   - Removed max_retries comparison from return value
   - Always returns `True` on success

#### Testing
Created comprehensive unit tests in `test_queue_retry_unlimited.py`:
- ✅ Test retry after 5 failures (previously would have stopped)
- ✅ Test retry after 10 failures (proves unlimited retries)
- ✅ Test increment_retry_count doesn't check max
- ✅ Test manual permanent failure with schedule_retry=False

All tests pass successfully.

---

### Part 2: Wikipedia MusicBrainz Search Integration

#### Modified Files
- `app.py`
- `templates/downloads.html`

#### New API Endpoint: `/api/upcoming-releases/search-musicbrainz`
**Location**: `app.py` lines 14235-14378

**Functionality**:
1. Accepts artist and album name from Wikipedia releases
2. Searches MusicBrainz release-group API for matches
3. For each match (up to 5):
   - Fetches release group details
   - Gets representative release with full tracklist
   - Returns track information (title, position, duration)
4. Implements rate limiting (1 second sleep between API calls) to respect MusicBrainz guidelines

#### New API Endpoint: `/api/queue/add-batch`
**Location**: `app.py` lines 10480-10545

**Functionality**:
- Accepts array of track items to add to queue in a single request
- Processes each item and tracks success/failure
- Returns detailed results including failed track names
- Significantly faster than sequential API calls for releases with many tracks

#### UI Changes
**Button Change**: "Download" → "Search" with MusicBrainz icon
**Modal**: Bootstrap modal displaying search results with accordion layout
**Security**: Fixed XSS vulnerability using data attributes instead of inline onclick handlers

---

## Security
- ✅ **0 CodeQL vulnerabilities found**
- Fixed XSS vulnerability in onclick handlers
- Proper input validation in batch endpoint
- Rate limiting for MusicBrainz API compliance

---

## User Workflow

### After Changes
1. User navigates to Upcoming Releases tab
2. Sees Wikipedia releases with "Search" button
3. Clicks Search → Modal opens with MusicBrainz search results
4. Views release matches with full track listings
5. Clicks "Download All Tracks" → All tracks added to queue via batch endpoint
6. Failed searches retry every 30 minutes indefinitely
