# Instrumental standout block + instrumental weight penalty

## Symptom

An instrumental version of a track (e.g. "Beware (instrumental)") with
massive raw listener counts scored 80.4 and hit a +3.80 z-score, and earned
5★ purely through the popularity Standout upgrade — even though single
detection correctly returned `Single: LOW` for it.  The instrumental stole a
5★ slot that belonged to a real vocal track.

Two problems:

1. **The Standout bypass**: the `z_standout` popularity confirmation and the
   artist top-% `popularity_marked` award are the only paths that could give
   a track 5★ without a single-detection source.  Instrumentals (which
   `should_skip_single_detection` already excludes from detection) were not
   excluded from these popularity paths.
2. **No statistical handicap**: a massive instrumental score mathematically
   buries the vocal tracks on the album via the album/artist z-scores.

## Fix 1 — Instrumental standout block (surgical)

- `services/catalog/album_classification_service.py` — new
  `is_instrumental_track_title()` helper (whole-word `instrumental` marker:
  "(instrumental)", "[Instrumental]", "- Instrumental", "(Full
  Instrumental)").
- `services/enrichment/single_detection_service.py` — the `z_standout`
  block refuses instrumental titles (`reasons.append("instrumental_version")`).
- `services/popularity/scan_stage_runner.py` — instrumental titles never get
  `popularity_marked = True` in either the album-level marking loop or the
  VA `_mark_track_artist_top_band` path, and never get the medium→high
  popularity-marking bump.
- `services/popularity/stages/finalise_stage.py` — `_has_z_standout_source`
  refuses instrumental titles (legacy rows that predate the gate).

The track keeps its full popularity score and its era-cap rating — only the
standout upgrade is blocked, so it lands at 4★ via the existing era caps.

## Fix 2 — Instrumental weight penalty (statistical)

- `services/popularity/popularity_config.py` — new
  `get_instrumental_weight_penalty()` (default `0.8`, i.e. 20% reduction),
  `single_detection.instrumental_weight_penalty`.
- `services/popularity/popularity_math.py` —
  `calculate_combined_popularity_score` gains `is_instrumental_track` +
  `instrumental_weight_penalty`; the Last.fm weight is reduced by the
  penalty fraction BEFORE the z-score (mirroring the live penalty, applied in
  both the normal and dynamic-weight paths).
- `services/popularity/stages/track_stage.py` — `_score_track_popularity`
  accepts `is_instrumental_track`, computed from the raw title at both
  scoring call sites; the config knob is read live.
- `services/popularity/pipeline.py` — scan-config log line surfaces
  `inst_pen`.

## Config

```yaml
single_detection:
  instrumental_weight_penalty: 0.8  # Last.fm weight fraction for instrumental versions (default: 0.8)
```

Surfaced on the Config page (Score Adjustments, next to Live Weight Penalty)
and saved via `static/js/config.js`.

## Tests

`tests/test_instrumental_standout_block_and_weight_penalty.py`:
- `TestInstrumentalTitleDetection` — marker patterns + whole-word guard.
- `TestInstrumentalWeightPenaltyConfig` — default 0.8, custom, zero-disables.
- `TestInstrumentalStandoutBlock` — the exact reported case (80.4 score,
  huge composite z) stays `single=low` / `z_standout=False`; the vocal twin
  keeps its standout/high.
- `TestStarRatingInstrumentalBlock` — legacy `popularity_z_standout` rows and
  `popularity_marked` never give an instrumental 5★; a vocal track keeps 5★.
- `TestInstrumentalWeightPenaltyScoring` — instrumental scores below its
  vocal counterpart; penalty 0 → identical score.
