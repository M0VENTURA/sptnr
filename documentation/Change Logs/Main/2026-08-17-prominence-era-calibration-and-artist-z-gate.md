# Prominence-based era calibration + artist-z 4★ gate

## What changed

A forced second scan reproduced the exact same Eat-the-Elephant-style skew.
Root cause: the era model's `M_peak` / `R_eff` were computed from
re-anchored ALBUM-RELATIVE medians, which collapse every album's median to
~50 by construction — so a 900k-listener album and a 50k-listener album both
classified `era=peak` with the same 5★ allowance, regardless of raw
listener volume.

## Fix 1 — raw-listener prominence benchmark (`services/popularity/popularity_math.py`, `finalise_stage.py`)

### Symptom

- *Eat the Elephant* (50k–136k LF listeners) → `era=peak`, `R_eff=1.00`
- *Mer de Noms* (600k–900k) → same `era=peak` after the two-pass deferral

Because `final_score`/`popularity` are album-relative by design (album
median + scaled-MAD → median 50), the era gate could never see cross-album
magnitude — the exact signal it needs.

### Fix

`_build_album_model` now prefers a **raw-listener prominence benchmark**:
per-album median of a log-scaled blend of `lastfm_listeners` +
`listenbrainz_listens` (persisted per track). `M_peak` = max album
prominence; `R_eff` = `min(1.0, (album_prominence / M_peak)^2)` — the
power-law amplification restores separation that the log scale compresses
(10x listeners = ~16 points, so a raw 0.82 ratio would still clear the
peak-era 0.75 boundary; squaring puts a 10x gap in `solid` and ~20x in
`minor`).  Falls back to the re-anchored score medians when no listener
data exists (legacy rows / single-album artists / test fixtures).

Because the listener counts persist to the DB, a FORCED re-scan loads the
catalogue-wide benchmark from the persistent cache at Album 1's rating time
(the two-pass defers rating to the artist-section close) — no pre-pass
scrobble gathering needed.

## Fix 2 — artist-z hard minimum for 4★ (`finalise_stage.py`, Config page)

### Symptom

4★ was purely album-relative (`album_z >= 0.5`), so the top 2–3 tracks of
ANY 12-track album qualified for 4★ regardless of catalogue prominence —
*Eat the Elephant*'s top tracks outranked *Mer de Noms*' mid tracks even at
10% of their listener volume.

### Fix

`_album_z_band_star` now accepts `artist_scores` and applies a **4★
artist-z hard minimum** (`single_detection.star_4.artist_z`, default 1.0):
a track must clear BOTH the album-z 4★ band AND the artist-catalogue z gate
to earn 4★.  A track that tops a weak album but sits mid-catalogue falls to
3★.  The gate only fires when a REAL distinct catalogue exists (more valid
artist scores than album scores) — a single-album artist keeps the pure
album band.

Config: `templates/pages/config.html` (4★ card now has an Artist Z input,
default 1.0) + `static/js/config.js` collects `star4_artist_z`.

## Files

- `services/popularity/popularity_math.py` — `album_prominence_score`,
  `album_prominence_median`, `row_get_lf`, `row_get_lb`.
- `services/popularity/stages/finalise_stage.py` — prominence benchmark in
  `_build_album_model` (power-amplified R_eff, `benchmark_source`), 4★
  artist-z gate in `_album_z_band_star` + `_assign_stars`, `star4_artist_z`
  threshold.
- `templates/pages/config.html` + `static/js/config.js` — `star4_artist_z`
  surfaced on the Config page.
- Tests: `tests/test_prominence_era_benchmark.py` (prominence era
  separation, catalog-wide M_peak invariant, 4★ artist-z gate, prominence
  helper math).
