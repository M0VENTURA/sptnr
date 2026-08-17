# Fix raw-vs-blended scale mismatch for LB-less tracks (false 5★ outliers)

## What changed

Single-source (Last.fm-only) tracks were scored on the **absolute log scale**
while multi-source (LF + LB) tracks were scored on the **album-relative
scale**, mixing two incompatible scales inside one album.

### Symptom (Stray Kids "NOEASY")

Sub-unit parenthetical titles — `Gone Away (HAN, Seungmin & I.N)`,
`Surfin' (Lee Know, Changbin & Felix)`, `Red Lights (Bang Chan & Hyunjin)` —
fail the ListenBrainz tracklist match, returning **0 listens**. The
single-source fallback then passed the raw absolute Last.fm score
(79.5–87.8) into the final score instead of the album's blended 45–65 band.
With the album median at 50 that produced z-scores of +3.3 to +3.5, which the
star-rating gate treated as runaway hits and forced to ★★★★★ — a 93.2k-listener
track outranking the 373.7k-listener title track.

### Fix (`services/popularity/popularity_math.py`)

1. **Last.fm is now scored album-relatively for every track with an album
   distribution** — the `listenbrainz_listens > 0` gate on the album-relative
   path was removed. An LB-less track's raw score is now on the same
   album-relative scale as its blended album-mates.

2. **The "strongest absolute evidence" floor now only applies to a genuine
   multi-source blend (≥ 2 independent absolute components).** For a
   single-source LF-only track (LB missing, and age derives from LB so it is
   0 too) the floor used to re-inflate the raw log score back above the
   album-relative re-map — recreating the very mismatch it was meant to
   prevent. With one source, the album-relative score is the honest score.

Also: the singles-pass stored-score Log-MAD audit now carries
`_raw_combined` for re-blended tracks so the album-relative normalization can
re-map a stale raw-scale stored score instead of leaving it as an outlier.

## Files

- `services/popularity/popularity_math.py` — album-relative LF for LB-less
  tracks; multi-source-only absolute floor.
- `services/popularity/stages/track_stage.py` — stored-audit re-blends set
  `_raw_combined` (participate in album-relative normalization).
- `tests/test_popularity_math.py` — `TestLbMissingFallbackAlbumRelativeScale`
  (LB-less → album-relative; multi-source floor preserved; no-distribution
  absolute fallback unchanged).
