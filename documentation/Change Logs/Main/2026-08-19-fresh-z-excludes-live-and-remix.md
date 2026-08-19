# Fresh album z-distributions exclude live AND remix tracks

## Symptom

Singles detection's `z_composite` / Standout Single fallback computed its
fresh album listener distribution WITHOUT excluding remix titles, while the
DB-stored stats paths (`_filter_bonus_rows` → `is_bonus_track_title`,
which matches `\bremix\b`) and the star-rating baseline both excluded them.
A deluxe album padded with a massive remix cut therefore saw a remix-polluted
album baseline in singles detection — crushing the core tracks' z — while
the same remix was excluded from the ratings math.

## Fix

`_build_album_listener_distributions` (the fresh in-memory album LF/LB
distribution used by the math-bridge) now excludes bonus/remix titles too
(`is_bonus_track_title`), matching every other distribution in the pipeline:

- `_filter_bonus_rows` / `calculate_album_stats` / `calculate_artist_stats`
  (DB-stored album/artist z)
- `calculate_album_listener_stats` (composite listener z DB fallback)
- `should_exclude_track_from_stats` (the `exclude_from_stats` flag)
- `_album_reference_scores` (album-relative re-map + star bands)

Live / acoustic / unplugged / demo / alternate tracks were already excluded
via `is_live_or_alternate_track_title` + `exclude_from_stats`; remix now
joins them.  The genuine-live-album fallback (fewer than 3 core tracks →
full tracklist) is unchanged.

No new scan pass is needed: the exclusion is a title filter on data already
in memory, applied at the existing math-bridge point.  The alternative — a
full artist-wide re-score pass after popularity — would re-scrape the APIs
and was deliberately avoided (the same result is achieved by making the
in-memory distribution match the stored stats paths).

## Files

- `services/popularity/stages/track_stage.py` — remix/bonus exclusion in
  `_build_album_listener_distributions`; import `is_bonus_track_title`.
- `tests/test_forced_pipeline_routing.py` — regression test: a remix cut
  with extreme counts is excluded from the fresh album distribution.
