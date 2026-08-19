# Global-first 5★ allocation: order-independent top billing

## Symptom

Battle Beast's forced scan: **Eden (raw score 80.4 — the highest in the
entire 91-track catalog) was rated 4★** while lower-scored tracks on
earlier-processed albums kept 5★ (King for a Day 78.1 → 5★, Last Goodbye
75.5 → 5★, Show Me How to Die 68.5 → 5★).  The album's per-album era/slot
gating demoted the catalog's #1 track.

## Root cause — sequential quota depletion

The pipeline finalises star ratings album by album in scan order.  The
per-album era gate awards 5★ against the ALBUM's relative distribution and
the era's per-album 5★ slot cap (peak-era `max_5star_slots=4`) demotes the
surplus by album-z.  Consequences:

1. **The album-relative re-anchor erases cross-album magnitude.**  Every
   album's median is re-centred at ~50, so Eden's raw 80.4 becomes an
   unremarkable album-z on a consistent album — while a lower raw score on
   a tighter album yields a bigger album-z and survives the cap.
2. **Order-of-processing bias.**  Earlier albums consume the 5★ share before
   a later album's stronger track arrives; a late-processed high-raw track
   is demoted to protect the "pyramid", even when it is the catalog #1.
3. **Entire collection classified `era=peak`** (raw-listener prominence
   benchmark missing) means the caps are uniform — the demotion isn't
   era-variation, it's the per-album slot gating.

## Fix — global-first 5★ allocation

The scan runner's artist-section pre-pass (`_flush_artist_star_ratings`,
which already runs AFTER every album of the artist is scored + persisted,
BEFORE any album is finalised) now:

1. **Ranks the ENTIRE catalog by RAW cross-album score** (`_raw_combined` —
   the pre-re-anchor weighted score, the only scale that preserves
   cross-album magnitude).
2. **Locks the artist's top tracks** (`global_5star_catalog_top_pct`,
   default 20% of the catalog) into a protected 5★ pool, restricted to
   genuine 5★ candidates (confirmed/standout singles, or raw score ≥
   `global_5star_min_raw_score` default 60) — so the pool is the band's
   biggest hits, never every album's top-3.  Live/bonus tracks excluded.
3. **`finalise_stage._assign_stars` honours the lock**: a locked track
   bypasses the per-album era gate and returns 5★ (still respecting the
   organic floor and live cap).
4. **The 5★ slot cap never demotes a locked track** — locked tracks are
   re-inserted as protected leaders before the weakest-by-album-z surplus
   is cut.

Eden (raw 80.4) and No More Hollywood Endings (76.3) now rank in the global
pool and keep 5★ regardless of album processing order.

## Files

- `services/popularity/scan_stage_runner.py` — `_compute_global_5star_locked_titles`
  pre-pass + `_artist_5star_locked_titles` + lock application in
  `_post_album_stars`.
- `services/popularity/stages/finalise_stage.py` — lock bypass in
  `_assign_stars` + slot-cap exemption.
- `tests/test_global_first_5star_allocation.py` — regression tests (raw-score
  ranking, live/bonus exclusion, era-gate bypass, slot-cap exemption).
