# Forced scan pipeline routing: math engine before single detection

## Symptom

Forced scans ("Check Missing"-adjacent forced popularity / singles /
combined runs) stepped on their own toes:

1. **Stale z-scores for the Standout Single fallback.**  Single detection's
   `album_z` / `artist_z` / `z_composite` were computed from DB-stored
   `final_score` / listener counts.  Mid-scan the per-track workers only
   flush their fresh scores at the END of the album (deferred persist), so
   the DB still held the PREVIOUS run's distribution.  A forced scan that
   refreshed playcounts (or a newly imported album) never shifted the
   median within the same run — the Standout Single fallback evaluated
   stale z-scores.

2. **Forced singles re-fetched every playcount.**  The runner's
   `if force or _pop_window <= 0: _pop_due = True` forced ALL tracks to
   re-fetch popularity even when they already had data — the opposite of
   smart gap-fill (hit the APIs only for tracks with NULL popularity).

## Architecture rule (now enforced)

**Math Engine → Single Detection → Star Ratings**, with the math computed
against the CURRENT scan's freshly gathered data:

```
gather raw counts → median/MAD/z-scores (fresh, in-memory)
  → single detection (uses the fresh z-scores / standout fallback)
  → assign stars (already deferred to artist-section close)
```

## Fix 1 — Fresh album distributions forwarded into single detection

`services/popularity/stages/track_stage.py`:

- New `_build_album_listener_distributions()` helper builds the album's
  FRESH Last.fm listener / ListenBrainz listen distributions from the
  current scan's prefetch (bonus/alternate/live cuts excluded, same rule as
  the star-rating baseline; live albums fall back to their full tracklist).
- `_score_track_popularity` now uses it (replacing the inline duplicate) —
  and also derives a bonus-excluded LB distribution for the realism /
  Log-MAD checks.
- `process_track` computes the fresh distributions and forwards them into
  `detect_single_for_track` (`album_lf_listeners` / `album_lb_listens`), so
  `z_composite` / the standout fallback evaluate the CURRENT scan's counts
  instead of the stale DB.  A singles pass additionally includes the
  track's own freshly gap-filled counts in its album distribution.

## Fix 2 — Forced singles = smart gap-fill

`services/popularity/scan_stage_runner.py`:

- The FORCE flag no longer sets `_pop_due` for singles passes.  Per-track
  gap-fill happens inside `process_track` (`_has_stored_popularity`):
  tracks WITH stored popularity are carried through unchanged (no API
  call), tracks with NULL popularity are fetched.  A fully-populated DB
  therefore skips the API instantly, even in forced mode — while forced
  singles detection and the math re-run still happen (the force flag still
  bypasses the singles-detection freshness gate).
- Only a config `popularity_skip_days: 0` (always rescan popularity)
  force-refreshes the whole album.

## Unchanged (already correct)

- **Forced popularity** (`popularity_only`): refreshes raw counts + math +
  stars, but singles detection and single-source persistence are skipped —
  existing MusicBrainz single tags are left alone.
- **Combined scan**: math runs once per album (album-relative re-map in the
  post-album pass), star ratings are deferred to artist-section close —
  never calculated twice.
- "Only update if playcounts changed" — `prefetch_artist_popularity` only
  writes rows whose counts changed; the per-track freshness gates reuse
  stored scores.

## Files

- `services/popularity/stages/track_stage.py` — fresh distribution helper +
  forwarding into single detection.
- `services/popularity/scan_stage_runner.py` — forced singles gap-fill.
- `tests/test_forced_pipeline_routing.py` — regression tests (fresh
  distributions forwarded; bonus cuts excluded; force does not set
  `_pop_due`).
