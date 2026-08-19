# Artist pipeline: single combined pass — stop scraping the APIs twice

## Symptom

A forced artist scan (e.g. Battle Beast, 8 albums / 91 tracks) took ~26
minutes and executed the full API scrape TWICE:

- `11:37 – 11:47` — **Pass 1: `Scan mode: Metadata`** (~10 min)
- `11:47 – 12:03` — **Pass 2: `Scan mode: Combined`** (~16 min)

Pass 1 resolved MusicBrainz metadata (album batch + per-track fallbacks +
writer backfill), tags and genres.  Pass 2 re-resolved the SAME MusicBrainz
metadata (forced scans bypass the MBID freshness gate) and re-fetched every
Last.fm / ListenBrainz popularity count — roughly 10 minutes of duplicate
API work.

## Root cause

`services/scanning/pipeline.py::_run_artist_scan_pipeline_inner` ran two
sequential `run_popularity_scan` calls for every artist:

1. `metadata_only=True` — resolves MB metadata, tags, genres.
2. combined — resolves MB metadata AGAIN (force bypasses the `_has_mbid`
   gate), then popularity + singles + covers + genres.

The standalone metadata pass was a **strict subset** of the combined pass:
`process_track` already resolves MusicBrainz metadata BEFORE popularity
scoring (`_resolve_track_mb_metadata` runs first), the album MB batch runs
in the combined pass, and genre collection runs for `not popularity_only
and not singles_detection_only` — i.e. in the combined pass.  The separate
metadata pass existed only to drive the dashboard's 4-stage progress model.

## Fix — one combined pass per artist

`_run_artist_scan_pipeline_inner` now runs the combined pass ONCE.  The
dashboard's 4-stage progress (Metadata / Popularity / Singles Detection /
Essentia) is preserved by splitting the combined pass's album loop into
bands — each album genuinely runs metadata resolution → popularity →
singles in that order, so the first quarter of albums map to "Metadata",
the middle half to "Popularity" and the last quarter to "Singles
Detection".

The standalone "Metadata" scan mode (dashboard `/scan/metadata`,
`run_popularity_mode(mode="metadata")`) is unchanged — this only removes
the redundant pre-pass inside the per-artist pipeline.

Expected impact: a forced artist scan takes roughly one combined pass
instead of metadata + combined — the Battle Beast run drops from ~26
minutes to ~16 (the combined pass itself), with the ~10-minute metadata
pre-pass eliminated.

## Files

- `services/scanning/pipeline.py` — remove the standalone metadata pass;
  single combined pass with metadata/popularity/singles stage bands.
- `services/scanning/pipelines/popularity_pipeline.py` — comment update for
  the merged stage model.
- `tests/test_full_scan_as_artist_pipeline.py` — fake pipeline emits the
  merged single-pass stage sequence; expectations updated (6 writes per
  artist instead of 14).
