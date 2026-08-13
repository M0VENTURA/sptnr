# Popularr Documentation

Welcome to the Popularr documentation repository. This folder contains comprehensive documentation for the Popularr music management system.

## 📁 Documentation Structure

```
documentation/
├── README.md                          # This file - Master index
├── USER_GUIDE.md                      # 📘 End-user guide (how the app works, scoring, settings)
├── POPULARR_ARCHITECTURE.md           # System architecture overview
├── Services/                          # Services layer documentation
│   ├── README.md                      # Services documentation index
│   ├── CONFIGURATION_GUIDE.md         # Complete configuration reference
│   ├── QUICK_REFERENCE.md             # Quick lookup guide
│   ├── SERVICES_CODE_QUALITY_REVIEW.md    # Code quality review
│   ├── FINAL_SUMMARY_COMPLETE.md      # Complete improvement summary
│   └── REMAINING_SERVICES_REVIEW.md   # Future work tracking
├── Change Logs/                       # Historical change logs
│   └── Main/                          # Main project changelogs
└── [Other documentation files...]     # Additional reference docs
```

## 🚀 Quick Start

### For New Users
1. Start with **[POPULARR_ARCHITECTURE.md](./POPULARR_ARCHITECTURE.md)** - Understand the system design
2. Review **[Services/CONFIGURATION_GUIDE.md](./Services/CONFIGURATION_GUIDE.md)** - Learn configuration options
3. Use **[Services/QUICK_REFERENCE.md](./Services/QUICK_REFERENCE.md)** - Fast lookup for common tasks

### For Developers
1. Read **[Services/SERVICES_CODE_QUALITY_REVIEW.md](./Services/SERVICES_CODE_QUALITY_REVIEW.md)** - Code quality standards
2. Review **[Services/FINAL_SUMMARY_COMPLETE.md](./Services/FINAL_SUMMARY_COMPLETE.md)** - Recent improvements
3. Check **[Services/REMAINING_SERVICES_REVIEW.md](./Services/REMAINING_SERVICES_REVIEW.md)** - Future work items

## 📚 Key Documentation

### Architecture & Design
- **[POPULARR_ARCHITECTURE.md](./POPULARR_ARCHITECTURE.md)** - Complete system architecture and design decisions

### Services Layer (Most Recent)
- **[Services/README.md](./Services/README.md)** - Services documentation index
- **[Services/CONFIGURATION_GUIDE.md](./Services/CONFIGURATION_GUIDE.md)** - All configurable parameters
- **[Services/QUICK_REFERENCE.md](./Services/QUICK_REFERENCE.md)** - Quick configuration lookup
- **[Services/SERVICES_CODE_QUALITY_REVIEW.md](./Services/SERVICES_CODE_QUALITY_REVIEW.md)** - Code quality analysis
- **[Services/FINAL_SUMMARY_COMPLETE.md](./Services/FINAL_SUMMARY_COMPLETE.md)** - Improvement summary
- **[Services/REMAINING_SERVICES_REVIEW.md](./Services/REMAINING_SERVICES_REVIEW.md)** - Future work tracking

### Change History
- **[Change Logs/Main/](./Change%20Logs/Main/)** - Main project changelogs
- **[scan_loop_stage_migration.md](./scan_loop_stage_migration.md)** - Scan loop migration notes
- **[popularity_pipeline_bootstrap_notes.md](./popularity_pipeline_bootstrap_notes.md)** - Pipeline bootstrap documentation

### Technical Notes
- **[lastfm_musicbrainz_split_notes.md](./lastfm_musicbrainz_split_notes.md)** - Last.fm/MusicBrainz separation
- **[musicbrainz_helpers_adjustment_notes.md](./musicbrainz_helpers_adjustment_notes.md)** - MusicBrainz helpers adjustments
- **[slskd_spotify_wikidata_split_notes.md](./slskd_spotify_wikidata_split_notes.md)** - Service separation notes
- **[new_structure_file_map.md](./new_structure_file_map.md)** - New project structure mapping
- **[missing_services_file_map.md](./missing_services_file_map.md)** - Missing services identification

## 🎯 Recent Improvements (July 2026)

### Configuration Centralization ✅
- **50+ hardcoded values** externalized to `config.yaml`
- **10 new configuration getter functions** created
- **8 service files** updated to use centralized config
- **Zero breaking changes** - fully backward compatible

### Enhanced Documentation ✅
- **11 service files** received comprehensive module docstrings
- **1,500+ lines of documentation** added
- **4 new documentation files** created
- **Complete configuration guide** with examples

### Impact Metrics
| Metric | Value |
|--------|-------|
| Files Modified | 15+ |
| Configuration Values Externalized | 50+ |
| Lines of Documentation Added | ~1,500+ |
| Compilation Errors | 0 |
| Breaking Changes | 0 |

## 🔧 Configuration Categories

All configuration is now centralized in `config.yaml`:

1. **Popularity Scoring** - Weights, standout detection, star ratings
2. **Genre Aggregation** - Source weights, synonym mappings  
3. **Queue Matching** - Thresholds, variants, duration tolerances
4. **slskd Timeouts** - Retry delays, state timeouts
5. **Last.fm Service** - Cache settings, API limits
6. **Filesystem** - Supported audio formats
7. **Wikidata** - Entity disambiguation terms

See **[Services/CONFIGURATION_GUIDE.md](./Services/CONFIGURATION_GUIDE.md)** for complete details.

## 📖 Additional Resources

### Project Structure
- **[new_structure_file_map.md](./new_structure_file_map.md)** - File organization
- **[missing_services_file_map.md](./missing_services_file_map.md)** - Service identification

### Migration Guides
- **[scan_loop_stage_migration.md](./scan_loop_stage_migration.md)** - Scan loop updates
- **[popularity_pipeline_bootstrap_notes.md](./popularity_pipeline_bootstrap_notes.md)** - Pipeline setup

### Technical Deep Dives
- **[lastfm_musicbrainz_split_notes.md](./lastfm_musicbrainz_split_notes.md)** - Service separation
- **[musicbrainz_helpers_adjustment_notes.md](./musicbrainz_helpers_adjustment_notes.md)** - Helper adjustments
- **[slskd_spotify_wikidata_split_notes.md](./slskd_spotify_wikidata_split_notes.md)** - Architecture decisions

## 🛠️ For Contributors

### Documentation Standards
- Module docstrings required for all public services
- Configuration values must be externalized (no hardcoding)
- Usage examples encouraged in all documentation
- Keep documentation up-to-date with code changes

### Code Quality
- Follow patterns in **[Services/SERVICES_CODE_QUALITY_REVIEW.md](./Services/SERVICES_CODE_QUALITY_REVIEW.md)**
- Review **[Services/REMAINING_SERVICES_REVIEW.md](./Services/REMAINING_SERVICES_REVIEW.md)** for improvement opportunities
- Maintain backward compatibility when possible

## 📅 Documentation Updates

**Last Major Update:** 2026-07-10  
**Status:** ✅ Services documentation complete  
**Next Review:** Ongoing - see remaining services tracking

---

**Questions?** Start with the **[Services/README.md](./Services/README.md)** for the most recent work, or **[POPULARR_ARCHITECTURE.md](./POPULARR_ARCHITECTURE.md)** for overall system understanding.
