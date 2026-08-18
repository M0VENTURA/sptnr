# Merge Last.fm counts across collab / multi-artist credits

## Symptom

RATATATA (BABYMETAL & Electric Callboy, 2024) scored only ~1.1k Last.fm
listeners — a tiny fraction of its real popularity — and dropped from 5★ to
1★ despite being one of the biggest metal tracks of the year.

## Root cause

Last.fm does not index the combined credit "BABYMETAL & Electric Callboy" as
an artist.  Instead, the track appears **separately under each artist's own
catalogue**: the "Electric Callboy" side shows ~1.1k listeners for RATATATA
while the "BABYMETAL" side holds the bulk of the scrobbles.  The popularity
pipeline queried only the primary artist's catalogue, so it found a match on
one side and reported only that fraction.

## Fix

`services/popularity/popularity_sources.py::get_aggregated_lastfm_popularity`
now detects collab credits (`&`, `x`, `and`, `×`, `+` between artist names)
and, **only when the primary catalogue finds no match**, splits the credit
and queries each sub-artist's catalogue for the same title, **merging the
listener/playcount totals**.  This recovers the collab's real audience
without double-counting when the full credit already resolves.

`services/popularity/popularity_matching.py::get_artist_lookup_candidates`
now also adds each sub-artist part so direct lookups (search, ISRC fallback,
provider queries) can target the individual catalogues.

The ListenBrainz side already aggregates collabs correctly via
`_recording_artist_mbids` + Work-level aggregation — no change needed there.

## Files

- `services/popularity/popularity_sources.py` — collab split + merged counts
  in `get_aggregated_lastfm_popularity`.
- `services/popularity/popularity_matching.py` — collab parts added to
  `get_artist_lookup_candidates`.
- `tests/test_collab_artist_lastfm_merge.py` — regression tests.
