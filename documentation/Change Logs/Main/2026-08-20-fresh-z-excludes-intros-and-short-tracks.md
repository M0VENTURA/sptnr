# Fresh album z-distributions exclude intros and sub-30s tracks (real fix)

## Symptom

The fresh in-memory album listener distribution — the math-bridge that gives
singles detection's `z_composite` / Standout Single fallback a CURRENT-scan
baseline — was NOT excluding intro / interlude / sub-30-second tracks, even
though live / acoustic / remix tracks were.  The user asked for intros and
tracks below 30 seconds to be removed from z-scoring at the same time as
live tracks.

## Root cause — the exclusion never actually ran

`_build_album_listener_distributions` read its exclusion reference from
`album_context["tracks"]` — but **`prepare_album_context` never populates a
`"tracks"` key** (it returns album metadata only).  In production the
reference was always empty, so the fresh distributions computed to `None`
and every consumer silently fell back to the DB-stored stats path.  The
live/remix exclusion added earlier was therefore dead code in production;
only the DB fallback (which excludes live/remix but applies a 90s floor and
does NOT exclude intro titles) was actually running.

## Fix

1. `services/popularity/scan_hooks.py` — `prepare_track_context` now writes
   its `exclude_from_stats` verdict (live albums, short interludes, intro /
   interlude title patterns, bonus/alternate titles) back onto the RAW track
   dict, which the scan runner references as `track_dicts` / `album_tracks`.
2. `services/popularity/stages/track_stage.py` —
   `_build_album_listener_distributions` now reads `album_tracks` (the
   enriched DB rows) instead of the never-populated `album_context["tracks"]`
   (kept as a fallback for tests / direct callers), and additionally excludes
   tracks below the 30-second floor via the new `_duration_below_floor`
   helper.  Both call sites (`_score_track_popularity` and the singles-
   detection bridge) pass `album_tracks`.

Net effect: singles detection's fresh album-z baseline now excludes live /
acoustic / remix / intro / interlude / alternate titles AND tracks shorter
than 30s — consistent with the star-rating baseline and the DB-stored stats
paths, and the fresh path actually runs instead of falling back.

## Files

- `services/popularity/scan_hooks.py` — `exclude_from_stats` written back
  onto the raw track dict.
- `services/popularity/stages/track_stage.py` — helper reads `album_tracks`,
  new `_duration_below_floor` (< 30s), both call sites pass `album_tracks`.
- `tests/test_forced_pipeline_routing.py` — regression tests: < 30s and
  intro tracks excluded; helper uses `album_tracks` when `album_context` has
  no tracks (the production shape).
