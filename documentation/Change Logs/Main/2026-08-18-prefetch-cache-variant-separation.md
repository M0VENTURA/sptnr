# Prefetch cache variant separation (live/acoustic/remix no longer inherit studio counts)

## Symptom

Remix, live and acoustic tracks were scored with the ORIGINAL version's
Last.fm popularity.  A local "(Live)" / "(Acoustic)" / "(Remix)" version of a
song received the studio recording's listener count (e.g. a 25k-listen live
cut inheriting the 80k studio recording's count), inflating the version's
score and distorting album/artist z-scores.

## Root cause

The bulk-cache prefetch fast-path (`prefetch_artist_popularity` →
`_lf_top_tracks_map` in `services/popularity/popularity_cache_service.py`)
keys Last.fm top-tracks by `normalize_for_aggregation(title)`, which
preserves hard version markers ("Beware (Live)" → `beware live`).  So the
cache map itself already keeps versions on distinct keys.

The bug was the **lookup side**: `track_stage.process_track` looked up the
prefetched entry using the CLEANED title (`title = lastfm_title`), which
strips ALL brackets via `clean_title(remove_brackets=True)`.  A local
"Beware (Live)" track therefore computed key `beware` — hitting the STUDIO
recording's cached entry (the sum of the plain title's own count, since the
map never summed the live count in).  The version track was scored with the
studio's popularity.

## Fix

`services/popularity/stages/track_stage.py` — the prefetch cache lookup now
keys on `raw_title` (which keeps the version markers intact) instead of the
cleaned `title`:

```python
_prefetch_entry = (prefetched_popularity or {}).get(
    normalize_for_aggregation(raw_title or title or "")
)
```

A local "(Live)" track now computes key `beware live` and hits ITS OWN
cached entry (the live cut's real, much smaller count), never the studio
sum.  This matches the existing search-path behaviour
(`get_search_aggregated_lastfm_popularity._collect` already applies
`title_variants_compatible`).

Also documented the hard-variant keying contract on
`_lf_top_tracks_map` (live/acoustic/instrumental/orchestral/remix/demo/
remaster markers never collapse; feat./radio-edit/single-version soft
markers still do).

## Tests

`tests/test_live_variant_popularity_separation.py`:
- `TestPrefetchCacheSeparatesVariants.test_variant_titles_have_distinct_prefetch_entries`
  — the prefetch map keys each hard variant separately (studio 25600,
  acoustic 600, instrumental 300, live 1200).
- `test_plain_track_lookup_does_not_inherit_variant_counts` — the studio
  entry is NOT 25600+600+300+1200 = 27700.
