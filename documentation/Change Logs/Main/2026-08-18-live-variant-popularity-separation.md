# Live / acoustic variants no longer inherit the studio track's popularity

## Symptom

Live, acoustic, instrumental and remix versions of a track were scored with
the **same Last.fm listener count as the studio recording**:

- "See You in Hell (acoustic)" showed 25.6k LF — identical to the canonical
  "See You in Hell"
- "See You in Hell (instrumental)" also 25.6k
- "Marching on Versailles (instrumental)" showed 18.9k — identical to the
  canonical version

These are different performances with different real audiences, and the
inflated counts pushed them to 4★/5★ ratings (and distorted the album's
popularity distribution).

## Root cause

`get_search_aggregated_lastfm_popularity` correlates published versions of a
song with the local title using `normalize_for_aggregation` + a
`fuzzy_match_score` (RapidFuzz `token_set_ratio`) gate of 0.90.
`token_set_ratio` is **word-subset insensitive**, so:

    "see you in hell acoustic" vs "see you in hell" → 1.0

The acoustic/instrumental/live/remix variants therefore matched the
canonical track's row and the search aggregation summed the studio
recording's 25k+ listeners into the alternate version's count.

The ListenBrainz side already guarded against this
(`_is_alternate_performance_title`), and the exact-key catalogue path
(`artist.getTopTracks` map keyed by `normalize_for_aggregation`) does not
collapse them — only the fuzzy **search** aggregation merged them.

## Fix

- `services/popularity/popularity_matching.py` — new
  `title_variants_compatible(a, b)` helper: hard version markers
  (live / acoustic / instrumental / orchestral / remix / demo / remaster /
  mix / intro) must **align** between two titles; soft markers
  ("version", "edit", "radio") may be absent from either side.  Mirrors the
  old system's `_title_variants_are_compatible`.
- `services/popularity/popularity_sources.py` —
  `get_search_aggregated_lastfm_popularity` now rejects a candidate whose
  title carries a hard variant marker the local title lacks (and vice
  versa), so "(acoustic)" gets only the acoustic version's count and the
  plain title gets only the canonical count.  `feat.` splits and soft
  annotations (Radio Edit, Single Version) still merge as before.

## Files

- `services/popularity/popularity_matching.py` — `title_variants_compatible`.
- `services/popularity/popularity_sources.py` — variant-compat gate in the
  search aggregation.
- `tests/test_live_variant_popularity_separation.py` — regression tests.
