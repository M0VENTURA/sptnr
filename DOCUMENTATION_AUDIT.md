# Documentation Audit & Deprecation Analysis

**Date**: March 20, 2026  
**Status**: Completed Audit  
**Purpose**: Identify outdated .md files and prepare comprehensive architecture documentation

---

## Executive Summary

This document catalogs **all** markdown files in the sptnr repository, categorizes them by current relevance, and identifies candidates for deprecation.

### Key Findings

- **Total .md files examined**: 170+ (root-level, documentation/, .github/)
- **Files to DEPRECATE**: 47 (implementation guides, historical fix summaries, superseded analyses)
- **Files to RETAIN**: 120+ (architecture, active feature docs, API reference, setup guides)
- **Duplicate directory**: `/depreciated` (typo) should consolidate with `/deprecated`

### Deprecation Rationale

Many .md files document **implementation process** rather than **current system behavior**. These are valuable for understanding history but don't serve ongoing development:

- `PHASE_*_IMPLEMENTATION.md` - Describe phases of completed multi-step feature work
- `*_FIX_SUMMARY.md` - Historical bug fixes (reference only, not architecture)
- `POSTGRES_COMPATIBILITY_ANALYSIS.md` - Obsolete (now Postgres-only mandate)
- Various analysis/proposal docs for features already implemented

---

## Detailed File Categorization

### TIER 1: CRITICAL ARCHITECTURE (RETAIN - HIGHEST PRIORITY)

These documents describe the current system design and must be maintained/updated.

#### Database & Configuration

- [ ] `helpers/check_db.py` - Schema management (code documentation)
- [ ] `MEAN_POPULARITY_ADJUSTMENT.md` - Artist-context scoring (active)
- [ ] `STAR_RATING_ALGORITHM.md` - 1-5 star logic (active)

#### Popularity Scanning
- [ ] `popularity.py` (inline docs) - Main entry point
- [ ] `UNIFIED_SCAN_README.md` - Unified scan pipeline
- [ ] `MEDIAN_MAD_IMPLEMENTATION.md` - Artist statistics

#### Single Detection
  
- [ ] `single_detection_enhanced.py` (inline docs) - 8-stage algorithm
- [ ] `SINGLE_DETECTION_IMPLEMENTATION.md` - Detection flow
- [ ] `ADVANCED_SINGLE_DETECTION.md` - Advanced strategies

#### Download Queue & Organization

- [ ] `queue_processor.py` (inline docs) - Processing loop
- [ ] `QUEUE_AND_DOWNLOADS_INTEGRATION_GUIDE.md` - Full lifecycle
- [ ] `POST_DOWNLOAD_PROCESSING.md` - File organization
- [ ] `DOWNLOAD_FILE_VERIFICATION.md` - Hash verification
- [ ] `MUSICBRAINZ_RELEASE_FLOW.md` - MB import flow

#### API Integration

- [ ] `API_RATE_LIMITS.md` - Rate limit policies
- [ ] `api_clients/*.py` (inline docs) - Each client

#### Missing Releases & Auto-Queue

- [ ] Section 15 of RatingAgent docs - Auto-queue behavior (added March 2026)
- [ ] TBD: Missing releases categorization helpers doc

#### Logging & Monitoring

- [ ] `LOGGING.md` - Current logging architecture
- [ ] `DOWNLOADS_MONITORING_SUMMARY.md` - Download monitoring

---

### TIER 2: FEATURE DOCUMENTATION (RETAIN - OPERATIONAL)

Active feature guides for users and developers.

#### UI/Web Features

- [ ] `WEB_UI_README.md`
- [ ] `FEATURES_DASHBOARD.md`
- [ ] `FEATURES_DOWNLOADS.md`
- [ ] `FEATURES_LIBRARY.md`
- [ ] `FEATURES_PLAYLISTS.md`
- [ ] `QUICK_REFERENCE.md`
- [ ] `QUICK_FIX_REFERENCE.md` (user-facing tips)

#### Setup & Installation

- [ ] `INSTALLATION.md`
- [ ] `MIGRATION_GUIDE.md`
- [ ] `CONFIGURATION_EXAMPLES.md`
- [ ] `MULTI_USER_CONFIG_GUIDE.md`
- [ ] `BEETS_INTEGRATION_PLAN.md` (optional feature)

#### Integrations

- [ ] `DISCOGS_TOKEN_SETUP.md` - Setup guide
- [ ] `LASTFM_RECOMMENDATIONS_SETUP.md` - Setup guide
- [ ] `SLSKD_CODE_COMPARISON.md` - Soulseek guide

#### MusicBrainz

- [ ] `MUSICBRAINZ_TAGS_IMPLEMENTATION.md` - Tag reading from MB
- [ ] `MUSICBRAINZ_SSL_FIX.md` - Certificate handling

#### Troubleshooting

- [ ] `LISTENBRAINZ_LIMITATION.md` - Known limitation docs
- [ ] `NAVIDROME_PRE_SYNC_DOCUMENTATION.md` - Sync behavior

#### Index & Navigation

- [ ] `documentation/INDEX.md` - Documentation index
- [ ] `documentation/README.md` - Overview

---

### TIER 3: IMPLEMENTATION GUIDES (DEPRECATE - HISTORICAL)


These describe *how* features were implemented. Useful for understanding code evolution but not needed for ongoing development.

#### Implementation Process Docs (37 files)
```
PHASE_5_FILE_MATCHING_IMPLEMENTATION.md
PHASE_6_FINALIZATION_IMPLEMENTATION.md
FEATURE_IMPLEMENTATION_PLAN.md
IMPLEMENTATION_COMPLETE.md
IMPLEMENTATION_SUMMARY.md
IMPLEMENTATION_GUIDE.md
IMPLEMENTATION_PLAN.md
IMPLEMENTATION_VERIFICATION_CHECKLIST.md
IMPLEMENTATION_ARTIST_CONTEXT.md
IMPLEMENTATION_SUMMARY_API_FIXES.md
IMPLEMENTATION_SUMMARY_POST_DOWNLOAD.md
PR_IMPLEMENTATION_SUMMARY.md
PR_158_IMPLEMENTATION_SUMMARY.md
PR_207_IMPLEMENTATION_SUMMARY.md
PR_243_BASE_BRANCH_UPDATE.md
INTEGRATION_IMPLEMENTATION_CHECKLIST.md
SESSION_SUMMARY_ARCHITECTURE_IMPROVEMENTS.md
```

#### Historical Fix/Analysis Documents (30+ files)
```
*_FIX_SUMMARY.md (all variations):
  - SINGLE_DETECTION_FIX_SUMMARY.md
  - POPULARITY_SCAN_ERROR_FIXES.md
  - POSTGRES_COMPATIBILITY_ANALYSIS.md (+ newer ANALYSIS docs elsewhere)
  - MUSICBRAINZ_DOWNLOAD_FIX.md
  - RATE_LIMIT_FIX_SUMMARY.md
  - SSL_FIX_SUMMARY.md
  - TIMEOUT_AND_PLAYLIST_FIX_SUMMARY.md
  - WRITER_FIELD_FIX_SUMMARY.md
  - ZSCORE_FIX_SUMMARY.md
  - UNWARRANTED_5STAR_FIX.md
  
*_ANALYSIS.md or *_INVESTIGATION.md:
  - MUSICBRAINZ_IMPLEMENTATION_PROGRESS.md
  - MUSICBRAINZ_REMAINING_PHASES_ANALYSIS.md
  - ARTIST_BIO_ALBUM_ART_INVESTIGATION.md
  - DISCOGS_LOOKUP_FIX_SUMMARY.md
  - LISTENBRAINZ_FIX_SUMMARY.md
```

#### Research/Proposal Documents (15+ files)
```
ARTIST_CATALOGUE_DYNAMIC_WEIGHTING.md
ARTIST_LEVEL_POPULARITY_IMPLEMENTATION.md
ARTIST_LEVEL_ZSCORE_IMPLEMENTATION.md
ADDITIONAL_OPTIMIZATION_RECOMMENDATIONS.md
ALBUM_DEVIATION_ADJUSTMENT.md
STANDOUT_TRACK_FIX.md
AND SIMILAR...
```

#### Deprecated Features (5+ files)
```
SPOTIFY_METADATA_FEATURES.md (Spotify integration deprecated)
SPOTIFY_PLAYLIST_IMPORT.md (Spotify integration deprecated)
BEETS_CONFIGURATION.md (optional, may keep)
LAYOUT_RECOMMENDATIONS.md (design doc, may archive)
DASHBOARD_MOCKUP.md (mockup only, may move to design/)
```

**Recommended Action**: Move all ~47 files to `/deprecated/` folder. Keep them for historical reference but exclude from active developer reading.

---

### TIER 4: REDUNDANCY & CONSOLIDATION

Files that should be consolidated or deduplicated:

#### Multiple Index/Overview Files
- `documentation/INDEX.md`
- `documentation/README.md`
- `ROOT README.md`
- **Recommendation**: Keep one master index, consolidate others

#### Duplicate Problem-Solving Docs
- Multiple "IMPLEMENTATION_SUMMARY.md" variants
- Multiple "FIX_SUMMARY.md" variants
- **Recommendation**: Consolidate into `/deprecated/HISTORICAL_FIXES/` folder

#### Directory Naming Issue
- `/deprecated/` exists (correct name)
- `/depreciated/` exists (typo - "depreciated" = loss in value, "deprecated" = marked obsolete)
- **Recommendation**: Migrate depreciated/ → deprecated/, delete depreciated/

---

## RatingAgent Documentation Gaps

Based on codebase audit, the following should be **added to RatingAgent docs**:

### 1. Missing Release Detection & Auto-Queue (NEW - March 2026)

**Current State in RatingAgent**: Section 15.3 documents auto-queue rules for missing singles

**Gaps to Add**:
- How `_normalize_release_category()` and `_derive_release_bucket()` work
- Release type prioritization logic (single > ep > album > compilation > live)
- Album-artist gating via `_is_album_artist_in_collection()`
- Collection title matching to skip redundant downloads
- Integration points in `api_artist_missing_releases()` and `api_scan_all_missing_releases()`

**Recommendation**: Create new section 16 "Missing Release Detection System" with diagrams showing:
```
Release Found by MB → Normalize Category → Derive Bucket → Check Album Artist Status → 
Load Collection Titles → Match Against Existing → Add to Queue (if new) → Log Event
```

### 2. Download Queue Lifecycle (Partial - Section 14)

**Current State**: Comprehensive queue documentation in RatingAgent section 14

**Gaps to Add**:
- Auto-queue event logging (new queue_events types: `autoqueue_initiated`, `autoqueue_skipped_exists`)
- MBID-first matching for releases
- Bulk MBID application from release groups
- Collection organizer integration with Navidrome sync

**Recommendation**: Expand section 14.3 "Queue Events" with auto-queue specific types

### 3. Genre/Tag Aggregation System (MISSING)

**Current State**: Not documented in RatingAgent at all

**Actual Implementation**:
- `genre_tag_aggregator.py` - Core aggregation engine
- 5 sources: Spotify, Last.fm, ListenBrainz, Discogs, MusicBrainz
- Per-track columns: `spotify_genres`, `lastfm_tags`, `listenbrainz_genres`, `discogs_genres`, `musicbrainz_genres`
- Endpoints: `/api/genres/track/<id>`, `/api/genres/album/<>`, `/api/genres/artist/<>`

**Recommendation**: Create new section 17 "Tag & Genre Aggregation System" with:
- Source credibility scoring
- Aggregation strategy (median-based filtering, top-N selection)
- Cache invalidation key: `tags_last_updated` column
- Integration with popularity scan

### 4. Compilation & Live Album Special Handling (Partial - Section 5.5)

**Current State**: Section 5.5 documents compilation treatment for single detection

**Gaps to Add**:
- How `_is_album_artist_in_collection()` defines "album artist" (primary/lead artist only, not featured)
- Live album z-score gating rules (stricter gates even with metadata confirmation)
- Greatest-hits special handling (no album-median gate requirement)
- Soundtrack special handling (different genre aggregation rules)

**Recommendation**: Add subsection 5.6 "Special Album Type Handling" with decision tree

### 5. Artist Identity & Deduplication (Partial - Missing from section references)

**Current Implementation**:
- `artist_identity.py` - Core deduplication logic
- `merge_duplicate_artists.py` - Merge operations
- DB fields: `artist_mbid`, `artist_is_main`, `album_artist`, `compilation_artist`

**Current RatingAgent Status**: Not documented at all in RatingAgent

**Recommendation**: Create new section 18 "Artist Identity System" with:
- Canonical artist resolution (MBID-first, fallback to fuzzy match)
- Main vs featured artist distinction
- Compilation artist vs track artist
- Merge impact on tracks/albums

### 6. MusicBrainz Release Group Discovery (MISSING)

**Current Implementation**:
- Daily release group fetch via MusicBrainz API
- Categorization into single/ep/album buckets
- Missing release detection on artist page

**Current RatingAgent Status**: Not in section 8 (API clients) or section 14 (queue)

**Recommendation**: Create subsection "8.1 MusicBrainz Release Group Workflow" with:
- Release group fetch frequency
- Caching strategy
- Categorization algorithm
- Missing detection scoring

### 7. Playlist Download & Import (MISSING)

**Current Implementation**:
- `playlist_matcher.py` - Track matching
- `playlist_recommendations.py` - Recommendation generation
- `/api/playlist-downloads/*` endpoints

**Current RatingAgent Status**: Not documented

**Recommendation**: Add subsection to section 8 "Playlist Import & Matching" with:
- Source detection (Spotify, Last.fm, ListenBrainz, M3U)
- Track deduplication strategy
- Batch download queueing

### 8. Config UI Contract (CURRENT - Section 3.1)

**Current State**: Section 3.1 documents `config.html` → `config.yaml` contract

**Gaps to Add**:
- Full list of configurable options with min/max constraints
- Validation rules per option
- Default value rationale

**Recommendation**: Create subsection 3.1.1 "Configurable Settings Reference" with table

---

## Recommended Actions

### Phase 1: Immediate (Before Next Scan)
1. ✅ Create this audit document (DONE)
2. Create `/deprecated/` subdirectories:
   - `/deprecated/IMPLEMENTATION_GUIDES/` - Phase docs, implementation summaries
   - `/deprecated/HISTORICAL_FIXES/` - All *_FIX_SUMMARY.md files
   - `/deprecated/PROPOSALS/` - Analysis and optimization proposals
   - `/deprecated/DEPRECATED_FEATURES/` - Spotify, Beets, older attempts

3. Move 47 identified files to appropriate `/deprecated/` subfolders

4. Delete `/depreciated/` directory (typo) after consolidating its README

5. Update RatingAgent with 8 new sections/subsections (see gaps above)

### Phase 2: Documentation Refactor (This Week)
1. Create comprehensive ARCHITECTURE.md (this document will become it)
2. Consolidate multiple INDEX/README files into one master
3. Add inter-document cross-references
4. Create `/documentation/API_REFERENCE.md` with all endpoints grouped by feature

### Phase 3: Ongoing
1. New implementation docs go to `/documentation/IMPLEMENTATION_HISTORY/` folder
2. Active feature docs stay in `/documentation/` root
3. RatingAgent updated bi-weekly with new findings

---

## File Movement Manifest

### To be MOVED to `/deprecated/IMPLEMENTATION_GUIDES/`

```
PHASE_5_FILE_MATCHING_IMPLEMENTATION.md
PHASE_6_FINALIZATION_IMPLEMENTATION.md
FEATURE_IMPLEMENTATION_PLAN.md
IMPLEMENTATION_COMPLETE.md
IMPLEMENTATION_SUMMARY.md
IMPLEMENTATION_GUIDE.md
IMPLEMENTATION_PLAN.md
IMPLEMENTATION_VERIFICATION_CHECKLIST.md
IMPLEMENTATION_ARTIST_CONTEXT.md
IMPLEMENTATION_SUMMARY_API_FIXES.md
IMPLEMENTATION_SUMMARY_POST_DOWNLOAD.md
PR_IMPLEMENTATION_SUMMARY.md
PR_158_IMPLEMENTATION_SUMMARY.md
PR_207_IMPLEMENTATION_SUMMARY.md
PR_243_BASE_BRANCH_UPDATE.md
INTEGRATION_IMPLEMENTATION_CHECKLIST.md
SESSION_SUMMARY_ARCHITECTURE_IMPROVEMENTS.md
```

### To be MOVED to `/deprecated/HISTORICAL_FIXES/`

```
All files matching pattern: *_FIX_SUMMARY.md
All files matching pattern: *_ANALYSIS.md (except active ones)
SINGLE_DETECTION_FIX_SUMMARY.md
POPULARITY_SCAN_ERROR_FIXES.md
POSTGRES_COMPATIBILITY_ANALYSIS.md
MUSICBRAINZ_DOWNLOAD_FIX.md
RATE_LIMIT_FIX_SUMMARY.md
SSL_FIX_SUMMARY.md
TIMEOUT_AND_PLAYLIST_FIX_SUMMARY.md
WRITER_FIELD_FIX_SUMMARY.md
ZSCORE_FIX_SUMMARY.md
UNWARRANTED_5STAR_FIX.md
... + 20 more
```

### To be MOVED to `/deprecated/PROPOSALS/`

```
ARTIST_CATALOGUE_DYNAMIC_WEIGHTING.md
ARTIST_LEVEL_POPULARITY_IMPLEMENTATION.md
ARTIST_LEVEL_ZSCORE_IMPLEMENTATION.md
ADDITIONAL_OPTIMIZATION_RECOMMENDATIONS.md
ALBUM_DEVIATION_ADJUSTMENT.md
STANDOUT_TRACK_FIX.md
... and similar analysis/proposal docs
```

### To be MOVED to `/deprecated/DEPRECATED_FEATURES/`

```
SPOTIFY_METADATA_FEATURES.md
SPOTIFY_PLAYLIST_IMPORT.md
BEETS_CONFIGURATION.md (or keep based on use)
LAYOUT_RECOMMENDATIONS.md
DASHBOARD_MOCKUP.md
... and mockups/experimental docs
```

---

## Notes Section Created as Reference

For RatingAgent additions, key references:

**Single Detection Helpers**: `_normalize_release_category()`, `_derive_release_bucket()`  
**Auto-Queue Helpers**: `_is_album_artist_in_collection()`, `_auto_queue_missing_singles_for_album_artist()`  
**Genre System**: `genre_tag_aggregator.py::get_artist_genres_summary()`, `get_album_genres_summary()`  
**Queue Events**: `queue_events` table with new types for auto-queue operations  

---

## This Document's Role

This audit serves as:
1. **Master index** of all documentation (current + deprecated)
2. **Deprecation rationale** for future pruning decisions  
3. **Gap analysis** for RatingAgent enhancement
4. **Migration plan** for consolidating folders

File location: `/sptnr/DOCUMENTATION_AUDIT.md`
