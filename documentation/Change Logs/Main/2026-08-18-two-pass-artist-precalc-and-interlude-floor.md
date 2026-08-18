# Two-pass artist pre-calculation + interlude/skit duration floor

## Symptom

Scan-order artifact on multi-album artists: the FIRST album scanned was rated
before the artist's catalogue profile existed, so its tracks got degenerate
`artist_z` values and missed the artist top-band upgrade.  Mudvayne's
*L.D. 50* is the canonical example: *Dig* (530k plays) received
`artist_z = +0.11` and missed the `artist top band` marking, while later
albums (*Mudvayne*, *The New Game*) had their singles upgraded to HIGH
confidence with large `artist_z` values (*Dull Boy* at `+2.12` on 93k plays).

Separately, ambient interludes / skits (Monolith, Golden Ratio, Mutatis
Mutandis, Recombinant Resurgence, Lethal Dosage on *L.D. 50*) compressed the
album median to ~50 and shrank the variance, dragging real tracks
(*Severed*, *Prod*, *Pharmaecopia*) down.

## Fix 1 — Two-pass artist pre-calculation

- `services/popularity/scan_stage_runner.py` — a **pass-1 artist pre-scan**
  runs at each artist-section boundary, BEFORE any of the artist's albums are
  scored.  It combines the artist's albums queued for this scan with the
  stored scores of albums outside the section (re-anchored to the
  album-relative scale) and injects them as `options.artist_stats_override`.
- `services/popularity/stages/track_stage.py` — `process_track` forwards the
  override to `detect_single_for_track`.
- `services/enrichment/single_detection_service.py` —
  `detect_single_for_track` accepts `artist_stats_override` and uses it for
  `artist_z` (falls back to the DB when absent), so the first album's tracks
  are z-scored against the full catalogue instead of an empty DB.

## Fix 2 — Interlude / skit duration floor

- `services/popularity/popularity_config.py` — new
  `get_exclude_from_median_below_seconds()` (default 90s, `statistics.*`).
- `services/catalog/album_classification_service.py` —
  `should_exclude_track_from_stats` gains `duration` /
  `exclude_below_seconds`; tracks shorter than the floor are excluded from
  the stats baseline (0 disables).
- `services/popularity/scan_hooks.py` + `scan_stage_runner.py` — pass the
  track duration through.
- `services/popularity/popularity_stats_service.py` — the DB-backed
  `calculate_album_stats` / `calculate_artist_stats` /
  `calculate_album_listener_stats` queries now select `duration` and apply
  the same floor via `_filter_bonus_rows`.

## Config

```yaml
statistics:
  exclude_from_median_below_seconds: 90  # Ignore interludes < 1:30 for album median/MAD
```

Surfaced on the Config page (Statistics card) and saved via
`static/js/config.js`.

## Files

- `services/popularity/scan_stage_runner.py` — pass-1 artist pre-scan.
- `services/enrichment/single_detection_service.py` — artist-stats override.
- `services/popularity/stages/track_stage.py` — override forwarding.
- `services/popularity/popularity_config.py` — statistics getter.
- `services/catalog/album_classification_service.py` — duration floor.
- `services/popularity/scan_hooks.py`, `popularity_stats_service.py` —
  duration threading + DB queries.
- `templates/pages/config.html`, `static/js/config.js` — Statistics card.
- `tests/test_two_pass_artist_stats_and_interlude_filter.py` — regression
  tests.
