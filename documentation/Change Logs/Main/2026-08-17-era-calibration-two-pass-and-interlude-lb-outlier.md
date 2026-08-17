# Two-pass era calibration (catalog-wide M_peak) + short-interlude LB outlier filter

## What changed

Two scoring-normalization fixes for the "Eat the Elephant" style skew,
where the first album scanned of an artist got an uncalibrated era while
later albums were damped, and a short ambient interlude outranked every
single because an inflated ListenBrainz count was weighted at 55%.

## Fix 1 — two-pass era calibration (`services/popularity/scan_stage_runner.py`)

### Symptom

The scanner rated each album's star ratings immediately after that album's
tracks scored.  Because the DB is populated progressively during a run,
the FIRST album of an artist was era-classified against a catalogue
containing only its own fresh scores:

- **Eat the Elephant (Album 1/5)** → `M_peak = 50.0` (its own re-anchored
  median) → `era=peak`, `R_eff=1.00` — every track rated without damping.
- **Mer de Noms / Thirteenth Step (Albums 4-5/5)** → the DB now contained
  Eat the Elephant's stored scores → `M_peak` reflected the grown catalogue
  → damped to `era=solid` with `R_eff=0.56`.

Order-dependent era classification meant the same album's ratings depended
on where it sat in the scan queue.

### Fix

Per-album star posting is now **deferred to the artist-section boundary**:

- The album loop (pass 1) scores + persists every album of the artist and
  queues each album into `_artist_pending_albums[artist]`.
- `_close_artist_section` (pass 2) flushes all of the artist's deferred
  star postings (`_flush_artist_star_ratings`) BEFORE the essential
  collection is written.  By that point every album of the artist is in the
  DB, so the 3-step album-scaling model resolves **catalog-wide M_peak**
  before ANY of the artist's albums is era-classified.

Result: all albums of an artist share the same catalog-wide M_peak and the
same era classification regardless of scan order — the first album scanned
no longer wins an uncalibrated `era=peak`.

## Fix 2 — short-interlude ListenBrainz outlier filter

### Symptom (the "DLB" spike)

`DLB` — a short ambient piano interlude on *Eat the Elephant* — recorded
**20,640 ListenBrainz listens** (higher than every single on the record)
against only **45.7k Last.fm listeners** (among the lowest on the album).
With LB weighted at 55% the raw LB sub-score hit 95.8, overriding the LF
reality and flagging the interlude as the album's top track (Score 64.4,
`album_z=1.52`, ★★★★★) — above the lead single *The Doomed*.

### Fix (`services/popularity/popularity_math.py`, `track_stage.py`, `popularity_config.py`)

A short track whose LB/LF ratio sits far above the album's median LB/LF
relationship is now treated as a recording-MBID artifact: the LB is rejected
and the track scores on Last.fm alone.

- `is_interlude_lb_outlier(...)` — compares the track's LB/LF ratio against
  the album's median LB/LF ratio.  Flagged when the track is under
  `max_duration_s` (default 180s), LB ≥ `min_lb` (default 500), and the
  ratio exceeds the album median by `ratio_factor`× (default 3.0).
- Wired into the fresh popularity score and the singles-pass stored-score
  re-audit (so a previously-scored interlude is corrected on the next
  singles scan).
- Config under `single_detection`: `interlude_lb_outlier_enabled`,
  `interlude_lb_max_duration_s`, `interlude_lb_ratio_factor`,
  `interlude_lb_min_count`.

DLB re-blends from 64.4 → ~30.0 (LF-only), putting it where a 45.7k-listener
interlude belongs.

## Files

- `services/popularity/scan_stage_runner.py` — deferred per-album star
  posting to the artist-section boundary (`_artist_pending_albums`,
  `_flush_artist_star_ratings`); genre-refresh preserved for single-album
  scans.
- `services/popularity/popularity_math.py` — `is_interlude_lb_outlier`.
- `services/popularity/popularity_config.py` — `get_interlude_lb_outlier_config`.
- `services/popularity/stages/track_stage.py` — interlude filter wired into
  fresh + stored scoring paths (`_score_track_popularity`, stored re-audit).
- `tests/test_interlude_lb_outlier.py`, `tests/test_two_pass_mpeak_calibration.py`.
