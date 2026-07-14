# SPTNR Architecture & Process Connection Guide

**Last Updated**: March 20, 2026  
**Purpose**: Complete technical reference for how every component connects

---

## Table of Contents

1. [Core Architecture](#core-architecture)
2. [Data Flow Diagrams](#data-flow-diagrams)
3. [Module Dependency Graph](#module-dependency-graph)
4. [Process Lifecycle](#process-lifecycle)
5. [API Endpoint Categories](#api-endpoint-categories)
6. [Database Schema Overview](#database-schema-overview)
7. [External API Integrations](#external-api-integrations)

---

## Core Architecture

### Layered Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    WEB UI (Flask)                         │
│  - Templates (Jinja2)                                     │
│  - Static Assets (JS/CSS)                                 │
│  - REST API Endpoints                                     │
└──────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────┐
│               SERVICE LAYER (app.py routes)              │
│  - Request handling & response formatting                │
│  - Session management                                     │
└──────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────┐
│          ORCHESTRATION & BUSINESS LOGIC LAYER             │
│  ┌───────────────┬────────────────┬────────────────┐     │
│  │  Popularity   │  Single        │  Download      │     │
│  │  Scanning     │  Detection     │  Management    │     │
│  │               │                │                │     │
│  │ popularity.py │ single_detec   │ queue_         │     │
│  │               │ tion_enhanced  │ processor.py   │     │
│  └───────────────┴────────────────┴────────────────┘     │
│  ┌───────────────┬────────────────┬────────────────┐     │
│  │  MusicBrainz  │  Compilation   │  Playlist      │     │
│  │  Integration  │  Management    │  Matching      │     │
│  │               │                │                │     │
│  │ musicbrainz   │ compilation    │ playlist_      │     │
│  │ _import.py    │ _manager.py    │ matcher.py     │     │
│  └───────────────┴────────────────┴────────────────┘     │
└──────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────┐
│              HELPER & UTILITY LAYER                       │
│  - Config management  (config_helpers.py)                │
│  - Metadata reading   (metadata_reader.py)               │
│  - Scan helpers       (scan_helpers.py)                  │
│  - Genre aggregation  (genre_tag_aggregator.py)          │
│  - Tag writing        (mp3scanner.py)                    │
│  - Logging            (logging_config.py)                │
└──────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────┐
│            DATABASE ABSTRACTION LAYER                     │
│  - database_abstraction.py (PostgreSQL only)             │
│  - helpers/check_db.py (Schema management)               │
└──────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────┐
│               DATA STORAGE LAYER                          │
│  - PostgreSQL Database (sptnr.db)                         │
│  - Config files (config.yaml)                            │
│  - Downloaded files (/downloads folder)                  │
│  - Music library (/music folder)                         │
└──────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────┐
│            EXTERNAL SERVICES (via API clients)            │
│  - MusicBrainz  - Last.fm  - ListenBrainz  - Discogs     │
│  - Navidrome    - AudioDB  - Soulseek      - Cover Art   │
└──────────────────────────────────────────────────────────┘
```

---

## Data Flow Diagrams

### 1. Popularity Scan Flow

```
popularity_scan() [Entry Point]
    ↓
[For each artist in library]
    ↓
get_track_metadata() [Fetch from DB/MP3]
    ↓
[Parallel API calls to 4 sources]
    ├→ Last.fm: track.getInfo(), getTopTags()
    ├→ ListenBrainz: get_listen_count(), get_recording_tags()
    ├→ Spotify (deprecated): get track popularity (legacy)
    └→ AudioDB: get_artist_genres()
    ↓
[Calculate weighted scores]
    ├→ Normalize each source to 0-100
    ├→ Apply configurable weights (lastfm:30%, listenbrainz:35%, age:25%, spotify:10%)
    └→ Get artist context statistics
    ↓
[Apply mean popularity adjustment]
    ├→ Calculate artist mean, stddev
    ├→ Compute z-score: (track_score - artist_mean) / stddev
    ├→ Convert to 0-100: 50 + (z * 16.7)
    └→ Apply time decay for pre-2005 releases
    ↓
save_to_db() [Write scores to track records]
    ↓
detect_single_for_track() [Parallel single detection]
    ↓
calculate_artist_stats() [Update artist catalog statistics]
    ↓
assign_star_ratings() [1-5 based on popularity z-scores + single metadata]
    ↓
Write id3/Vorbis tags [Update audio file metadata]
```

### 2. Single Detection Flow (8-Stage)

```
detect_single_for_track()
    ↓
Stage 1: Pre-filter [Exclude live, acoustic, remix, demo, etc]
    ├─ If title contains forbidden words → LOW confidence
    └─ Otherwise → Continue
    ↓
Stage 2: Album popularity gate [Score vs album median]
    ├─ If compilation/greatest-hits → SKIP gate
    ├─ If top-3 tracks in album → CONTINUE
    ├─ If > (median - 0.5*stddev) → CONTINUE
    └─ Otherwise → LOW confidence
    ↓
Stage 3: Metadata API queries [Query enabled sources]
    ├→ Call discogs.is_single() [Highest confidence source: video track / dedicated single]
    ├→ Call musicbrainz.is_single() [Release type = single]
    ├→ Call lastfm.check_track_as_single() [Last.fm single flag]
    └→ (Spotify deprecated)
    ↓
Stage 4: Version count [Check global versions of this track]
    ├─ 1-2 versions worldwide → Likely single
    ├─ 3+ versions → Likely album track with variants
    └─ Impact confidence scoring
    ↓
Stage 5: Z-score gating [Artist context scoring]
    ├─ Medium confidence: z-score ≥ 0.6
    ├─ High confidence: z-score ≥ 1.0
    ├─ Remastered tracks: BYPASS z-score, use metadata only
    └─ Compilation: BYPASS negative z-score requirement
    ↓
Stage 6: ISRC matching [Same performance detection]
    ├─ Get track ISRC from MusicBrainz
    ├─ Query: does this ISRC appear on other releases?
    └─ If same track different versions → single confirmation
    ↓
Stage 7: Title/duration matching [Version filtering]
    ├─ Fuzzy title match: >= 92% similarity
    ├─ Duration match: ±2 seconds
    └─ Filters alternate versions (radio edit, remix, etc)
    ↓
Stage 8: Compilation handling [Special case logic]
    ├─ Greatest-hits album: NO album-median gate
    ├─ Compilation: USE metadata sources primarily
    ├─ Live album: STRICT z-score even with metadata
    └─ Apply special genre rules
    ↓
store_single_detection_result()
    ├─ Write is_single (bool)
    ├─ Write single_confidence (user|high|medium|low)
    ├─ Write single_sources (JSON list)
    └─ Write title_similarity_to_base (float 0-1)
```

### 3. Download Queue Lifecycle

```
User Action Triggers Queue Addition
    ├─ Manual: User clicks "Download" on search result
    ├─ Auto: Missing-single auto-queue fired (new March 2026)
    └─ Batch: Playlist import or folder processing
    ↓
add_to_queue()
    ├─ Check: Does track already exist in collection?
    │  ├─ Yes (normalized title match) → Skip queue insertion
    │  └─ No → Continue
    └─ Insert into download_queue table
       ├─ status = 'queued'
       ├─ priority = (0-10)
       ├─ retry_count = 0
       └─ import_group = mbid_<release_mbid> OR folder_hash
    ↓
queue_processor.py [Main loop - runs every N seconds]
    ├─ Get queued items (ordered by priority, then timestamp)
    └─ For each item:
       ↓
       [Status: searching] → slskd.search()
           ├─ Query Soulseek: artist + title
           ├─ Collect results (user, file paths, bitrates)
           ├─ Score candidates:
           │  ├─ Format priority: FLAC > MP3-320 > MP3-VBR
           │  ├─ Title similarity: >= 85% match to Navidrome
           │  └─ Artist similarity: >= 75% match
           ├─ Check downloaded_files hash table (no re-downloads)
           ├─ Emit queue_events: search_started, search_completed
           └─ If 0 results → mark_failed(), retry_count++
       ↓
       [Status: downloading] → slskd.download_file()
           ├─ Initiate download from best candidate user
           ├─ File lands in /downloads/staging folder
           ├─ Emit queue_events: download_started
           └─ Poll until complete or timeout
       ↓
       [Status: importing] → download_file_manager.organize_file()
           ├─ Verify file hash (ensure completeness)
           ├─ Extract metadata from file
           ├─ Match to Navidrome metadata (artist, album, title)
           ├─ Move file from staging to music library:
           │  └─ /music/{artist}/{album}/{track}.mp3
           ├─ Read ID3/Vorbis tags from file
           ├─ Write to database:
           │  ├─ Insert track record if new
           │  ├─ Update track with Navidrome match
           │  ├─ Write tags: spotify_genres, lastfm_tags, etc
           │  └─ Update imported file hash table
           ├─ Emit queue_events: import_started, import_completed
           └─ Update queue item status
       ↓
       [Status: completed | failed]
           ├─ completed: Track now in music library
           ├─ failed (retry_count < max_retries) → requeue with backoff
           └─ failed (retry_count >= max_retries) → archived
```

### 4. MusicBrainz Release Import Flow

```
User searches MusicBrainz for release
    ↓
Search MB Release Group API
    ├─ Query: artist name + optional release title
    └─ Return: list of release groups with types (single, album, ep, compilation, etc)
    ↓
User selects release → clicks "Import"
    ↓
musicbrainz_import.py::start_mb_import()
    ├─ Fetch full release details from MB API
    ├─ Extract:
    │  ├─ Release MBID
    │  ├─ Track list with ISRCs
    │  ├─ Artist credits
    │  ├─ Release date
    │  └─ Tags/genres
    ├─ Create MB download queue item
    │  ├─ status = 'matching'
    │  ├─ import_group = mbid_<release_mbid>
    │  └─ Store full release JSON
    └─ Emit event: mb_import_started
    ↓
musicbrainz_release_manager.py [Background processing]
    ├─ For each track in release:
    │  ├─ Fetch ISRC from MB
    │  ├─ Match to Navidrome tracks:
    │  │  ├─ First attempt: ISRC exact match
    │  │  ├─ Fallback: Title + artist fuzzy match
    │  │  └─ Fallback: Title only fuzzy match
    │  ├─ Assign confidence level based on match type
    │  └─ Store in queue_match table
    ├─ Create import_group folder in /tmp with all matched tracks
    └─ Update release status = 'ready_for_finalization'
    ↓
musicbrainz_finalizer.py [Finalization step]
    ├─ For each import_group:
    │  ├─ Copy matched files from music library to staging
    │  ├─ Write MB tags to each file:
    │  │  ├─ MBID
    │  │  ├─ Album MBID
    │  │  ├─ Artist MBID
    │  │  ├─ Release tags
    │  │  └─ Genre from MB
    │  ├─ Move files to final destination in music library
    │  ├─ Update database: set musicbrainz_release_mbid
    │  ├─ Trigger popularity rescan for updated tracks
    │  └─ Log completion
    ├─ Emit event: mb_import_completed
    └─ Delete staging files
```

### 5. Missing Release Detection & Auto-Queue (NEW - March 2026)

```
User navigates to Artist page
    ↓
api_artist_missing_releases()
    ├─ Query MusicBrainz: all releases for artist
    ├─ For each MB release:
    │  ├─ _normalize_release_category(category/type)
    │  ├─ _derive_release_bucket() → determines single|ep|album|compilation|live
    │  ├─ Check local collection: do we have this release?
    │  │  ├─ Album MBID match → We have it
    │  │  ├─ Title similarity >= threshold → We probably have it
    │  │  └─ Otherwise → Missing
    │  └─ Categorize as missing with type/date info
    ├─ Group missing by category bucket
    ├─ Return buckets to UI [single, ep, album, compilation, live]
    └─ OPTIONALLY trigger auto-queue
         ├─ _is_album_artist_in_collection(artist)
         │  ├─ True: This is a primary artist in collection
         │  └─ False: This is featured-only artist
         ├─ For album artists only:
         │  ├─ For each detected SINGLE in missing list:
         │  │  ├─ Load existing track titles from collection
         │  │  ├─ Normalize search title
         │  │  ├─ If title match exists → SKIP (already have)
         │  │  └─ Otherwise → add_to_queue() with auto-queue flag
         │  └─ Log: a/o, s<missing?>, a<add_to_queue>
         └─ Continue processing other artists
    ↓
Dashboard shows "X Missing Releases" banner
    ├─ Filterable: All / Collection Artists / Recommended
    └─ Each missing item has:
       ├─ MB release link
       ├─ "Search Discogs" fallback button
       ├─ "Import" button (queues for download)
       └─ Type badge (single/album/ep)
```

---

## Module Dependency Graph

### Core Processing Chain

```
app.py (Flask routes)
  ├─→ popularity.py (Main scan engine)
  │    ├─→ popularity_helpers.py (Scoring, artist context)
  │    ├─→ single_detector.py (Single detection dispatch)
  │    │    └─→ single_detection_enhanced.py (8-stage algorithm)
  │    ├─→ api_clients/ (All 4+ external APIs)
  │    ├─→ database_abstraction.py (Write results)
  │    └─→ genre_tag_aggregator.py (Collect tags)
  │
  ├─→ unified_scan.py (Orchestrate multiple scan types)
  │    ├─→ popularity_scan()
  │    ├─→ scan_mp3_import.py (Import new files)
  │    └─→ queue_processor.py (Download processing)
  │
  ├─→ musicbrainz_import.py (MB import orchestration)
  │    ├─→ musicbrainz_release_manager.py (Match tracks)
  │    ├─→ musicbrainz_finalizer.py (Write tags & move files)
  │    └─→ api_clients/musicbrainz.py (MB API calls)
  │
  ├─→ queue_processor.py (Download queue main loop)
  │    ├─→ api_clients/slskd.py (Soulseek search/download)
  │    ├─→ download_file_manager.py (Organize files)
  │    ├─→ download_queue_manager.py (Queue CRUD)
  │    ├─→ download_retry_manager.py (Retry logic)
  │    ├─→ download_file_verification.py (Hash verification)
  │    └─→ download_folder_grouping.py (Folder logic)
  │
  ├─→ compilation_manager.py (Compilation handling)
  │    ├─→ artist_identity.py (Artist deduplication)
  │    └─→ merge_duplicate_artists.py (Merge operations)
  │
  ├─→ api_clients/ (External integrations)
  │    ├─→ musicbrainz.py (MB API)
  │    ├─→ lastfm.py (Last.fm API)
  │    ├─→ audiodb_and_listenbrainz.py (LB + AudioDB)
  │    ├─→ discogs.py (Discogs API)
  │    ├─→ navidrome.py (Navidrome sync)
  │    ├─→ slskd.py (Soulseek)
  │    └─→ coverartarchive.py (Album art)
  │
  └─→ helpers/
       ├─→ config_helpers.py (Load/parse config.yaml)
       ├─→ db_utils.py (PostgreSQL utilities)
       ├─→ check_db.py (Schema management)
       ├─→ metadata_reader.py (MP3/FLAC tag reading)
       ├─→ scanning.py (Scan state management)
       ├─→ logging_config.py (Structured logging)
       └─→ helpers.py (Utility functions)
```

---

## Process Lifecycle

### Scan Type Orchestration

```
┌─────────────────── MANUAL START (User Click) ──────────────────┐
├─ /scan/start → Full scan (popularity + singles)                │
├─ /scan/popularity → Popularity scan only                        │
├─ /scan/singles → Singles detection only                         │
├─ /scan/navidrome → Sync with Navidrome library                  │
└─ /scan/combined → Popularity + singles + navidrome              │
↓
unified_scan.py::unified_scan_pipeline()
├─ acquire_scan_lock() [Prevent concurrent scans]
├─ load_scan_state() [Resume interrupted scans]
├─ For each scan type requested:
│  ├─ popularity_scan() [entire library]
│  │  ├─ For each artist:
│  │  │  ├─ popularity() calculation
│  │  │  ├─ single_detection() for each track
│  │  │  └─ save_to_db() with progress update
│  │  └─ calculate_artist_stats() after all tracks
│  │
│  ├─ scan_mp3_import.py [New files in music folder]
│  │  ├─ Find new .mp3/.flac files
│  │  ├─ Extract metadata
│  │  └─ Insert new track records
│  │
│  └─ queue_processor() [Download queue background loop]
│     ├─ runs continuously (every N seconds)
│     └─ processes queued items lifecycle
│
├─ release_scan_lock()
└─ Return scan_status: completed | failed | interrupted

CONTINUOUS (Background Process)
├─ queue_processor() loop [from queue_processor.py]
│  ├─ Every 30 sec (configurable):
│  │  ├─ Get next 'queued' item
│  │  ├─ Slskd search → download → organize
│  │  └─ Update queue_events log
│  └─ runs as daemon thread
│
├─ downloads_watcher() [from downloads_watcher.py]
│  ├─ Monitors /downloads folder for new files
│  ├─ Auto-organize based on folder grouping
│  └─ Trigger post-download processing
│
└─ daily_musicbrainz_refresh()
   ├─ Once per day: fetch new release groups for collection artists
   ├─ Auto-queue new singles for album artists
   └─ Update missing_releases cache
```

---

## API Endpoint Categories

### Scanning & Status
- `GET /scan/start`, `/scan/popularity`, `/scan/singles`, `/scan/navidrome`, `/scan/combined`
- `GET /scan/status` → Current progress
- `GET /api/scan-logs` → Event stream
- `POST /api/scan/artist` → Single artist rescan

### Artist Management
- `GET /api/artist/missing-releases` → Missing albums/singles
- `POST /api/artist/scan-all-missing-releases` → Batch scan
- `GET /api/artist/stats` → Artist popularity statistics
- `GET /api/artist/<artist>` → Artist detail page
- `GET /api/artist/<artist>/similar` → Similar artists

### Popular Tracks/Albums
- `GET /api/album/<artist>/<album>` → Album detail
- `GET /api/track/<track_id>` → Track detail
- `GET /api/genres/track/<id>`, `/api/genres/album/<>`, `/api/genres/artist/<>`

### Download Management
- `POST /api/queue/add` → Add item to queue
- `GET /api/queue/status` → Queue item counts
- `GET /api/queue/events` → Queue audit trail
- `GET /downloads` → Downloads page UI

### MusicBrainz
- `POST /api/musicbrainz/search` → Search MB releases
- `POST /api/musicbrainz/download` → Import release
- `GET /api/musicbrainz/downloads` → Active imports

### Upcoming Releases
- `GET /api/upcoming-releases` → Listed releases with filter params
- `GET /api/upcoming-releases?collection=true` → Collection artists only
- `GET /api/upcoming-releases?recommended=true` → Recommended only

---

## Database Schema Overview

### Core Tables

**tracks**
- `track_id`, `artist`, `album`, `title`, `duration`
- `popularity` (0-100), `popularity_confidence` (float 0-1)
- `is_single` (bool), `single_confidence` (user|high|medium|low)
- `single_sources` (JSON list)
- `star_rating` (1-5)
- `spotify_genres`, `lastfm_tags`, `listenbrainz_genres`, `discogs_genres`, `musicbrainz_genres` (JSON)
- `tags_last_updated` (timestamp)
- `artist_mbid`, `album_mbid`, `musicbrainz_release_mbid`

**albums**
- `album_id`, `artist`, `album_title`
- `album_mbid`
- `release_type` (album | single | ep | compilation | live)
- `release_date`
- `track_count`

**artists**
- `artist_id`, `artist_name`
- `artist_mbid`
- `is_album_artist` (bool - primary artist in collection)
- `popularity` (avg across catalog)

**artist_stats**
- `artist`, `track_count`
- `mean_popularity`, `median_popularity`, `stddev_popularity`
- `mad_popularity` (median absolute deviation)
- `mean_popularity_adjusted` (pre-2005 time-adjusted)

**download_queue**
- `id`, `artist`, `title`, `status` (queued|searching|downloading|importing|completed|failed)
- `priority` (0-10)
- `retry_count`
- `import_group` (mbid_<mbid> or folder_hash)
- `createdvfat_at`, `updated_at`

**queue_events**
- `queue_id` (FK)
- `event_type` (search_started|search_completed|download_started|download_completed|import_started|import_completed|etc)
- `timestamp`
- `details` (JSON - extra context)

**missing_releases** (cache)
- `artist`, `release_mbid`, `title`
- `release_type` (bucket)
- `release_date`
- `last_checked`

**downloaded_files**
- `file_hash` (SHA256)
- `file_path`
- `artist`, `album`, `title`
- `import_status` (pending|completed|moved)
- `imported_at`

---

## External API Integrations

### API Client Architecture

All external API calls go through `api_clients/` modules:

```
api_clients/__init__.py
├─ session (global requests.Session with retries)
├─ timeout_safe_session (strict timeout variant)
└─ rate limit management
   └─ MusicBrainz: respects Retry-After header
   └─ Discogs: enforces 0.35 sec min between requests (token rate limit)

api_clients/musicbrainz.py
├─ is_single() → Check if release type is single
├─ get_genres() → Get tags/genres for release
├─ get_suggested_mbid() → Lookup artist MBID
├─ lookup_and_save_artist_mbid() → Cache artist → MBID mapping

api_clients/lastfm.py
├─ get_track_info() → Scrobbles, listener count
├─ check_track_as_single() → Single flag from Last.fm
├─ get_track_tags() → User-contributed tags
├─ get_similar_artists() → Similar artist list
├─ get_artist_top_tags() → Artist-level tags
└─ get_recommendations() → Track recommendations

api_clients/audiodb_and_listenbrainz.py
├─ get_listen_count() → ListenBrainz listen count
├─ get_user_listen_count() → Per-user stats
├─ get_recording_tags() → LB tags for MBID
├─ get_artist_tags() → Artist tags from LB
├─ get_recommendations() → LB recommendations
└─ (AudioDB shared client for artist data)

api_clients/discogs.py
├─ get_comprehensive_metadata() → Full Discogs lookup
├─ is_single() → Discogs single detection (highest confidence)
├─ has_official_video() → Video track detection
├─ get_genres() → Genre/style field
└─ Rate limit: 0.35 sec/request enforced

api_clients/navidrome.py
├─ fetch_all_playlists() → Get user playlists
├─ get_song() → Get track metadata
├─ star_track() / unstar_track() → Rating sync
├─ start_scan() → Trigger Navidrome library scan
└─ build_artist_index() → Artist → tracks mapping from Navidrome

api_clients/slskd.py
├─ search() → Initiate Soulseek search
├─ get_results() → Poll search results
├─ download_file() → Start file download
├─ get_active_downloads() → Current transfers
├─ cancel_download() → Cancel transfer
└─ Rate limit: Configurable based on slskd server
```

---

## Configuration & Customization Points

**config.yaml** controls:

```yaml
navidrome:
  base_url, user, pass, music folder

weights:
  spotify (deprecated), lastfm, listenbrainz, age

single_detection:
  zscore_medium_threshold, zscore_high_threshold

features:
  album_skip_days, clamp_min/max, title_sim_threshold
  strict_spotify_matching, use_lastfm_single

api_integrations:
  lastfm: {enabled, api_key}
  listenbrainz: {enabled}
  discogs: {enabled, token}  # Required for single detection
  musicbrainz: {enabled}
  audiodb: {enabled, api_key}

downloads:
  folder, quality_filter, format priorities

watcher:
  scan_interval, navidrome_sync_wait, auto_import_enabled
  auto_popularity_scan

slskd:
  enabled, web_url, api_key

qbittorrent:
  enabled, web_url, username, password

logging:
  level, file, console output
```

---

## Key Implementation Notes

### Database Strategy: Postgres-Only
- All new code uses PostgreSQL (`%s` placeholders)
- No SQLite fallbacks in active code paths
- Fail-fast if PostgreSQL unavailable

### MBID-First Matching
- Always check MBID fields first before name-based searches
- Cache MBID → name mappings in database

### Genre/Tag Aggregation
- 5 sources: Spotify, Last.fm, ListenBrainz, Discogs, MusicBrainz
- Per-track columns store source data separately
- UI aggregates and displays with source attribution

### Queue Events Audit Trail
- Every queue action appended to queue_events (never edited)
- Replay events to reconstruct historical state
- No in-place status updates

### Auto-Queue Rules (March 2026)
- Only albums artists (not featured-only)
- Skip tracks already in collection (normalized title match)
- Single-type releases only
- Logged in queue_events with auto-queue flag

---

## Entry Points for Common Tasks

| Task | Starting File | Function |
|------|---|---|
| Full scan | `app.py` | `/scan/start` route |
| Popularity calc | `popularity.py` | `popularity_scan()` |
| Single detect | `single_detection_enhanced.py` | `detect_single_enhanced()` |
| Download item | `queue_processor.py` | main loop |
| MB import | `musicbrainz_import.py` | `start_mb_import()` |
| Artist page | `app.py` | `/artist/<name>` route |
| Missing releases | `app.py` | `/api/artist/missing-releases` route |
| Config update | `helpers/config_helpers.py` | `save_config()` |

---

**Document Version**: 1.0  
**Architecture Stable Since**: March 2026  
**Last Major Change**: Auto-queue system for missing singles (March 2026)
