# L.D. 50 regression fix: intro stripping + log-listens artist_z + force floors

## Symptom

After the two-pass artist pre-calculation (2026-08-18), Mudvayne's *L.D. 50*
regressed: *Dig* (530k listeners) dropped from 4★ to 3★.  Root cause: the
*By the People, for the People* intro album — 14 spoken-word intro tracks at
~200 listens each — cratered the album floor, inflated ordinary tracks'
relative scores, and poisoned the artist baseline, so *Dig*'s artist_z fell
to -0.38.

Two interacting problems:

1. **Intro/interlude tracks were not stripped** — the duration floor (60s)
   missed ~40-60s spoken intros with titles like *"Dig Intro"*, and the
   title-based exclusion didn't exist yet.  Every intro sat in the same
   album/artist distribution as real songs.
2. **artist_z was computed from within-album score deltas** — on a
   "consistent album" (L.D. 50) every track is popular, so *Dig*'s
   album-relative score looked ordinary even though it is the artist's
   most-listened track by an order of magnitude.

## Fix 1 — Intro / interlude title stripping

- `services/popularity/popularity_config.py` — new
  `get_exclude_title_regex()` (default
  `(?i)(^intro$|\bintro$|interlude|\[intro\]|\(intro\))`), configured via
  `filters.exclude_title_regex`.
- `services/catalog/album_classification_service.py` —
  `should_exclude_track_from_stats` gains `exclude_title_regex`; matching
  titles are excluded from the stats baseline (empty disables).
- Default `statistics.exclude_from_median_below_seconds` lowered 90 → 60.

## Fix 2 — log-listens artist_z (absolute catalogue prominence)

- `services/popularity/scan_stage_runner.py` — the pass-1 pre-scan now ALSO
  collects the artist's raw Last.fm listen distribution (`_pre_listens` +
  new `_load_artist_db_listeners`) and injects it as
  `options.artist_listen_override`.
- `services/popularity/stages/track_stage.py` — forwards
  `artist_listen_override` to single detection.
- `services/enrichment/single_detection_service.py` — when the override is
  present, `artist_z` is computed on
  `log10(listens)` across the artist's global distribution:
  `z = (log10(listens) - median(log10(catalogue))) / max(MAD*1.4826, 0.25)`.
  This is immune to within-album normalization (the "consistent album"
  paradox).  Falls back to the score-based path when absent.

## Fix 3 — hard absolute force floors

- `services/popularity/popularity_config.py` — new
  `get_artist_force_star_percentiles()`:
  `single_detection.artist_top_percentile_force_5_star` (default 0.03, top
  3% → 5★) and `..._force_4_star` (default 0.10, top 10% → at least 4★).
- `services/popularity/stages/finalise_stage.py` — `_assign_stars` gains
  `artist_listen_distribution`; a track ranked in the artist's absolute top-N%
  by raw listeners is forced to 5★/4★, bypassing album_z gating but NEVER the
  live cap or user override.  `post_album_star_ratings` builds the
  distribution from this album's fresh results + stored rows for the rest of
  the catalogue.

## Config

```yaml
statistics:
  exclude_from_median_below_seconds: 60  # Ignore interludes < 1:00 for album median/MAD
filters:
  exclude_title_regex: "(?i)(^intro$|\\bintro$|interlude|\\[intro\\]|\\(intro\\))"
single_detection:
  artist_top_percentile_force_5_star: 0.03  # Top 3% raw listens → 5★
  artist_top_percentile_force_4_star: 0.10  # Top 10% raw listens → ≥ 4★
```

Surfaced on the Config page (Statistics card + Single Detection section) and
saved via `static/js/config.js`.

## Tests

`tests/test_two_pass_artist_stats_and_interlude_filter.py`:
- `TestIntroTitleExclusion` — "Dig Intro", "Intro", "(Interlude)",
  "[Intro]" excluded; "Introduction" kept; empty regex disables.
- `test_log_listens_artist_z` / `test_log_listens_artist_z_low_track` —
  Dig (530k) clears `artist_z > 2.0`; mid-catalogue track lands 0.3-2.0.
