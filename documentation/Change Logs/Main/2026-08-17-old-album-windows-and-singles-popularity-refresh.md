# Old-album rescan windows + singles scan popularity refresh

## What changed

Two related scan-behaviour features:

### 1. Old-album rescan windows (per scan type)

Albums released before a configurable age threshold are now treated as **old
albums** and use a longer, per-scan-type rescan window — their popularity
metrics change far less often, so they are rescanned less frequently.

New config (Config page → **Scan Behaviour**):

| Key | Default | Meaning |
|-----|---------|---------|
| `features.old_album_age_months` | 48 | Album age (months) that makes an album "old" |
| `features.album_old_album_skip_days` | 30 | Full-scan window for old albums |
| `features.popularity_old_album_skip_days` | 30 | Popularity-scan window for old albums |
| `features.singles_old_album_skip_days` | 30 | Singles-scan window for old albums |
| `features.metadata_old_album_skip_days` | 30 | Metadata-scan window for old albums |

Albums with no usable release year are treated as recent (normal window).

### 2. Singles scan refreshes stale popularity

A singles scan previously reused stored popularity and only scored tracks with
**no** data. Now, when an album is **outside the popularity rescan window**
(its popularity data is stale), the singles pass refreshes popularity for the
whole album — so a singles scan doubles as a catch-up popularity pass.

- The popularity window honours the old-album window above for old albums.
- A **forced** singles scan always refreshes (force bypasses the window).
- A popularity window of `0` (always rescan) also refreshes.
- Recent albums still reuse stored popularity (no extra API calls).

## Files

- `services/popularity/scan_stage_runner.py` — `_album_release_is_old()` helper;
  per-mode old-album skip windows; `refresh_popularity_if_due` computed for
  singles passes and threaded into per-track options.
- `services/popularity/stages/track_stage.py` — singles pass honours
  `refresh_popularity_if_due` (re-scores stored-popularity tracks instead of
  carrying the stored score).
- `templates/pages/config.html` — new Scan Behaviour inputs (old-album age +
  per-type old-album windows); new keys hidden from the generic Features card.
- `tests/test_old_album_windows.py` — `_album_release_is_old` + singles-pass
  refresh gating.
