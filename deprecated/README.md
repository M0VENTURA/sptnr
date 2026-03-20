# Deprecated / Historical Content

This `/deprecated/` folder contains:
1. **Deprecated Python files** - Old code no longer used in active development
2. **Historical documentation** - Organized by category (see below)

## Deprecated Python Files

### sptnr.py
**Status**: Archived (No longer used)

**Reason**: Original CLI rating tool. Functionality migrated to modern architecture:
- Popularity scoring → `popularity.py`
- Singles detection → `single_detector.py`
- Web interface → `app.py`, `server.py`
- Configuration → `config.yaml`

**Do NOT use** - reference only.

---

## Historical Documentation

Organized into 5 categories:

### 📋 FIX_SUMMARIES/
Bug fixes and patches (20+ files)  
**Use for**: Debugging, understanding past issues  
**Examples**: ALBUM_ART_DOWNLOAD_FIX.md, DISCOGS_PUNCTUATION_FIX.md

### 📘 IMPLEMENTATION_GUIDES/
Previous implementation plans (25+ files)  
**Use for**: Learning how features were built  
**Examples**: PHASE_5_FILE_MATCHING_IMPLEMENTATION.md, LAYOUT_RECOMMENDATIONS.md

### 💡 PROPOSALS_ANALYSIS/
Analyses and proposals (40+ files)  
**Use for**: Understanding problem space, design options  
**Examples**: MUSICBRAINZ_REMAINING_PHASES_ANALYSIS.md, OPTIMIZATION_RECOMMENDATIONS.md

### 📅 HISTORICAL_SESSIONS/
Session & PR documentation (20+ files)  
**Use for**: Pull request work, session context  
**Examples**: PR_158_IMPLEMENTATION_SUMMARY.md, SESSION_SUMMARY_*.md

### 👥 ARTIST_IDENTITY_LEGACY/
Archived artist system variants (13 files)  
**Use for**: Artist deduplication evolution  
**Examples**: ARTIST_ID_CACHING.md, ARTIST_LEVEL_ZSCORE_IMPLEMENTATION.md

---

## Find Current Documentation

**Active docs**: `/documentation/INDEX.md`  
**Architecture**: `COMPREHENSIVE_ARCHITECTURE.md` (root)  
**Project setup**: `INSTALLATION.md`

---

**Note**: These files are preserved for reference only. Active development documentation is in `/documentation/`.

