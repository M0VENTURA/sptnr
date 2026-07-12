
# Popularr System Architecture (Current State)

## Overview

This document describes the full architecture of the system after the major refactors, including:
- Navidrome scan pipeline
- API client separation
- Enrichment services
- Database structure
- Discogs split

---

# 1. High-Level Architecture

```
routes/
    ↓
services/scanning/
    ↓
services/enrichment/
    ↓
services/metadata (extractors)
    ↓
api_clients/
    ↓
db/repositories/
    ↓
PostgreSQL
```

### Core Principle

- API clients fetch data
- Services interpret data
- Pipelines orchestrate workflows
- Repositories persist data

---

# 2. Routes Layer

## Structure

```
routes/scan_routes/
routes/navidrome/
```

Routes:
- Trigger scans
- Control lifecycle (start/stop/status)
- Call pipelines

Compatibility shim:

```python
from routes.scans import scans_bp
```

---

# 3. Scanning Pipelines

```
services/scanning/pipelines/
```

Includes:
- artist_pipeline
- album_pipeline
- navidrome_pipeline
- popularity_pipeline
- essentia_pipeline

### Responsibilities

- Loop orchestration
- Resume + checkpoint logic
- Batching
- Hook execution

---

# 4. Navidrome Flow

## Structure

```
api_clients/navidrome.py
services/scanning/navidrome_service.py
services/scanning/metadata_extractor.py
services/scanning/payload_builder.py
services/scanning/navidrome_import.py
```

### Flow

```
NavidromeClient (API)
    ↓
navidrome_service
    ↓
metadata_extractor
    ↓
payload_builder
    ↓
navidrome_import
    ↓
db.repositories
```

---

# 5. Database Layer

```
db/
├── context.py
├── utils.py
├── schema.py
├── bootstrap.py
└── repositories/
```

### Repositories

- tracks
- artists
- genres
- navidrome
- library

### Rules

- Only repositories perform DB writes
- API clients never write directly

---

# 6. API Clients

```
api_clients/
```

Examples:
- Navidrome
- ListenBrainz
- AudioDB
- Apple Music
- AcousticBrainz
- Cover Art Archive
- Discogs (split)

### Rules

API clients MUST:
- Only perform HTTP
- Not contain business logic
- Not contain scoring

---

# 7. Enrichment Layer

```
services/enrichment/
```

### Purpose

Handles decision-making:
- fallback logic
- scoring
- matching
- prioritisation

### Services

- artwork_service
- listenbrainz_service
- discogs_service

---

# 8. Discogs Architecture

```
api_clients/discogs_http.py
services/enrichment/discogs_service.py
api_clients/discogs.py
```

### Split Responsibilities

**discogs_http.py**
- HTTP calls
- rate limiting
- retries
- circuit breaker

**discogs_service.py**
- single detection
- title matching
- cache usage
- metadata interpretation

**discogs.py**
- compatibility facade

---

# 9. Library Sync & Hooks

After scans complete:

```
run_post_navidrome_hooks()
```

Order:
1. album artist sync
2. library diff sync
3. MP3 import

---

# 10. Full End-to-End Flow

```
Route
 → Pipeline
 → Navidrome Service
 → API Clients
 → Metadata Extractor
 → Payload Builder
 → Import Service
 → DB Repositories
 → Database

+ Enrichment applied during scoring & matching
+ Post hooks executed after scan
```

---

# 11. Key Architecture Benefits

✅ Strict separation of concerns  
✅ Backward compatibility maintained  
✅ Scalable enrichment system  
✅ Clean API client structure  
✅ Modular pipelines  

---

# 12. Future Improvements

Recommended next step:

```
services/matching/
```

Separate:
- identity resolution
- track matching
from enrichment/ scoring logic.

---

# Summary

This architecture provides a scalable and maintainable system where:
- external APIs are isolated
- business logic is centralised
- pipelines are clean orchestration layers
- database operations are controlled

