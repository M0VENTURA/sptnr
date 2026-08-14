# Audit cleanup: beets removal + API client hardening (2026-08-14)

## Beets integration removed

The Beets integration (external tagger) has been retired.  Deleted:

- `routes/beets_routes.py` — the `/api/beets/*` blueprint
- `templates/pages/beets_integration.html` — the `/beets` page
- `/beets` page route in `routes/ui_routes.py`
- Beets JS (`updateAlbumWithBeets`, `updateArtistAlbumsWithBeets`,
  `updateAlbumsSequentially`) in `static/js/main.js`
- Blueprint registration in `helpers/app_bootstrap.py`
- `test_beets_status` in `tests/test_routes.py`; the auth-gate test now uses
  `/api/stats`

DB columns (`beets_mbid`, `beets_year`, `beets_album_artist`) and the
metadata-compare page that reads them are **kept** — they are metadata
fields, not part of the beets API integration.

## API client hardening

- **`api_clients/http_utils.py`** — the shared retry transport now honors the
  `Retry-After` header on 429/503 responses (seconds or HTTP-date), falling
  back to exponential backoff.  MusicBrainz and all shared-session clients
  now respect the server's rate-limit directive instead of hammering it.
- **`api_clients/lrclib.py`** — uses the shared `api_clients.session` instead
  of creating a per-call httpx client that was never closed (connection/TLS
  leak).
- **`api_clients/lastfm.py`** — `__all__` now lists only names actually
  defined/imported in the module (previously referenced symbols that live in
  `services.enrichment.lastfm_service`, which would raise `AttributeError`
  on `import *`).
- **`api_clients/musicbrainz_http.py`** — removed dead methods with zero
  callers: `browse_recording_by_release`, `search_release_groups_with_genres`,
  `get_artist_with_genres`, `get_recording_with_genres`.
- **`api_clients/acousticbrainz.py`** — removed (replaced by Essentia; only
  `old_system/` referenced it).
- **`routes/queue/queue_scoring.py`** — removed (dead duplicate of
  `services/queue/queue_scoring.py`, zero importers, duplicate
  `_score_soulseek_candidate`).
- **`routes/track_routes.py`** — `POST /api/track/<id>/rename-file` now
  builds the destination under MUSIC_ROOT from `downloads.file_name_format`
  (same convention as the album rename flow) and enforces containment; the
  previous implementation passed an empty destination (broken) with no
  commonpath check.
- **`routes/beets_routes.py`** — was also hardened with path containment
  before being removed (moot).

## Tests

- `tests/test_routes.py` / `tests/test_auth_gate.py` — no longer reference
  the removed beets API.
- `tests/test_queue_retry_scheduler_wiring.py` (prior) — unchanged.
