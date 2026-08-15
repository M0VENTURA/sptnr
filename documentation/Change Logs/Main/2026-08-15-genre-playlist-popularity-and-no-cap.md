# Genre Playlists: Popularity Ordering, No Cap, and Per-Album Refresh

## What changed

1. **Genre playlist tracks are now sorted by popularity (most popular first)**
   - `_create_genre_top_track_playlists` sorts each genre's playlist by
     `final_score` DESC (then stars DESC, then title) instead of stars-first.
   - The dedup winner-selection order (studio over live, main release over
     compilation, then stars, then popularity) is unchanged.

2. **Removed the top-N cap (was 500)**
   - Every qualifying 4★/5★ track is now included in the genre playlist.
   - Removed the `playlists.genre_playlists_top_n` config option from
     `finalise_stage`, the `config.html` UI, `config.js`, the scan-config
     log line in `pipeline.py`, and its test references.

3. **Genre playlists refresh at the end of every album scan where tracks of
   that genre changed**
   - New `refresh_genre_playlists_for_album(artist, album)` in
     `finalise_stage.py`: after an album's star ratings are posted, it
     determines which genres the album's qualifying tracks belong to and
     rebuilds only those genres' playlists (`only_genres` scoped pass).
   - Wired into `scan_stage_runner`'s per-album star posting so playlists
     stay current mid-scan instead of only at the very end.
   - The scoped pass skips the stale-file sweep so unrelated genre playlists
     are never touched; the scan-end full pass still handles deletion.
   - Best-effort: any failure is logged at DEBUG and never breaks the scan.

## Files touched

- `services/popularity/stages/finalise_stage.py`
- `services/popularity/scan_stage_runner.py`
- `services/popularity/pipeline.py`
- `templates/pages/config.html`
- `static/js/config.js`
- `tests/test_genre_playlists.py`
