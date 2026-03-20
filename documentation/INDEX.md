# sptnr Documentation Index

**Last Updated**: March 2026 | **Active Files**: 34 | **Historical Files**: 120+ (in `/deprecated`)

## Quick Navigation

### Getting Started
- **[README](README.md)** - Project overview and introduction
- **[Installation](INSTALLATION.md)** - Setup and deployment
- **[Configuration Examples](CONFIGURATION_EXAMPLES.md)** - Configuration templates
- **[Migration Guide](MIGRATION_GUIDE.md)** - Upgrade procedures
- **[Multi-User Configuration](MULTI_USER_CONFIG_GUIDE.md)** - Multi-user setup

### User Interfaces
- **[Web UI README](WEB_UI_README.md)** - Web interface guide
- **[Dashboard](FEATURES_DASHBOARD.md)** - Dashboard features
- **[Library](FEATURES_LIBRARY.md)** - Artist/album browser
- **[Downloads](FEATURES_DOWNLOADS.md)** - Downloads manager
- **[Playlists](FEATURES_PLAYLISTS.md)** - Playlist features

## Core Features

### Popularity & Scoring
- **[Star Rating Algorithm](STAR_RATING_ALGORITHM.md)** - Track rating system (1-5 stars)
- **[Mean Popularity Adjustment](MEAN_POPULARITY_ADJUSTMENT.md)** - Artist-context scoring
- **[Unified Scan](UNIFIED_SCAN_README.md)** - Complete scanning workflow

### Single Detection & Classification
- **[Single Detection](SINGLE_DETECTION_IMPLEMENTATION.md)** - Core algorithm
- **[Single Detection Guide](SINGLE_DETECTION_IMPLEMENTATION_GUIDE.md)** - Detailed documentation
- **[Artist Identity Rules](ARTIST_IDENTITY_RULES.md)** - Deduplication rules
- **[Artist Context](ARTIST_CONTEXT_README.md)** - Artist metadata system
- **[Artist Integration](ARTIST_IDENTITY_INTEGRATION.md)** - Integration patterns

### Download & Queue Management
- **[Download Queue](DOWNLOAD_QUEUE_SYSTEM.md)** - Queue architecture
- **[Queue Integration](QUEUE_AND_DOWNLOADS_INTEGRATION_GUIDE.md)** - Processing pipeline
- **[Queue Events](QUEUE_EVENTS_LOG_IMPLEMENTATION.md)** - Event logging
- **[Post-Download](POST_DOWNLOAD_PROCESSING.md)** - File organization and tagging
- **[File Verification](DOWNLOAD_FILE_VERIFICATION.md)** - Hash verification
- **[Downloads Monitoring](DOWNLOADS_MONITORING_SUMMARY.md)** - Real-time status

### Metadata & External APIs
- **[MusicBrainz Flow](MUSICBRAINZ_RELEASE_FLOW.md)** - Release import
- **[MusicBrainz Tags](MUSICBRAINZ_TAGS_IMPLEMENTATION.md)** - Tag synchronization
- **[Navidrome Metadata](NAVIDROME_METADATA_TAG_MANAGEMENT.md)** - Tag writing
- **[Last.fm Setup](LASTFM_RECOMMENDATIONS_SETUP.md)** - Last.fm integration
- **[Discogs Setup](DISCOGS_TOKEN_SETUP.md)** - Discogs API configuration

### System & Infrastructure
- **[API Rate Limits](API_RATE_LIMITS.md)** - Rate limiting reference
- **[Logging](LOGGING.md)** - Logging configuration

## Organization

### This Directory (`/documentation/`)
**34 active** markdown files covering features, setup, and APIs.

### `/deprecated/` Subdirectories
Historical documentation by category:
- **FIX_SUMMARIES/** - Bug fixes and patches
- **IMPLEMENTATION_GUIDES/** - Past implementation plans
- **PROPOSALS_ANALYSIS/** - Analyses and proposals
- **HISTORICAL_SESSIONS/** - Session/PR summaries
- **ARTIST_IDENTITY_LEGACY/** - Archived variants

### Root (`/sptnr/`)
Master references:
- **[COMPREHENSIVE_ARCHITECTURE.md](../COMPREHENSIVE_ARCHITECTURE.md)** - System design with diagrams
- **[DOCUMENTATION_AUDIT.md](../DOCUMENTATION_AUDIT.md)** - Complete file inventory
- **[CODEBASE_AUDIT_SUMMARY.md](../CODEBASE_AUDIT_SUMMARY.md)** - Code structure

## I want to...

- **...set up sptnr** → [Installation](INSTALLATION.md)
- **...understand popularity** → [Star Ratings](STAR_RATING_ALGORITHM.md) & [Mean Popularity](MEAN_POPULARITY_ADJUSTMENT.md)
- **...use single detection** → [Implementation](SINGLE_DETECTION_IMPLEMENTATION.md)
- **...manage downloads** → [Queue System](DOWNLOAD_QUEUE_SYSTEM.md)
- **...integrate MusicBrainz** → [Release Flow](MUSICBRAINZ_RELEASE_FLOW.md)
- **...see architecture** → [COMPREHENSIVE_ARCHITECTURE.md](../COMPREHENSIVE_ARCHITECTURE.md) (root)
- **...debug issues** → [Logging](LOGGING.md) & [Rate Limits](API_RATE_LIMITS.md)

## For Developers

See [COMPREHENSIVE_ARCHITECTURE.md](../COMPREHENSIVE_ARCHITECTURE.md) in root for:
- System architecture diagram
- Complete data flow diagrams
- Full module dependency graph
- 100+ key functions reference

---

**Questions?** Check [README](README.md) or [COMPREHENSIVE_ARCHITECTURE.md](../COMPREHENSIVE_ARCHITECTURE.md)
