# Soulseek matching: reject wrong-artist files (Orville Peck / The Fall of Troy)

## Symptom

The queue item **"Orville Peck - The Fall"** was matched on Soulseek and
downloaded from a peer as:

```
music\The Fall of Troy (post‐hardcore band)\[2020] Mukiltearth\
The Fall of Troy - Mukiltearth - 01 A Tribute to Orville Wilcox.flac
```

That is a completely different band's track — The Fall of Troy's "A Tribute
to Orville Wilcox", not Orville Peck's "The Fall".

## Root cause

Two weak gates in `services/downloads/download_pipeline_service.py::_score_result`
combined to accept the wrong file:

1. **Artist-evidence gate matched a first-name token anywhere in the path.**
   The gate collected every word in the remote path into `filename_tokens`
   and treated ANY single significant artist word present as evidence the
   artist was there.  The token "orville" *is* in the path — but only inside
   the track **title** ("A Tribute to **Orville** Wilcox").  A shared first
   name between a target artist and a different band's song title is not
   evidence the target artist is present.

2. **Title-substring fallback scored without artist affinity.**
   `_normalise(expected_title) in _normalise(filename)` is True — "the fall"
   is a substring of "the fall of troy".  That awarded +15 partial title
   credit, and combined with quality/queue bonuses (FLAC, free slot, empty
   queue, upload speed) reached the 30-point acceptance threshold.

## Fix

`_score_result` now requires artist evidence from the **artist segment** of
the candidate — the parsed artist field, plus the remote path with the
track-title tokens removed — instead of any token anywhere:

- `art_score >= 0.6` (parsed-artist similarity), **or**
- the full artist phrase as a substring of the artist scope, **or**
- at least TWO significant artist words appearing together in the
  artist-scope tokens (a lone first name no longer counts).

The +15 title-substring fallback is unchanged in effect but now only runs
after the artist gate has passed, so a same-worded title by a different band
is already rejected before the substring credit is considered.

## Files

- `services/downloads/download_pipeline_service.py` — `_score_result`
  artist-evidence gate scoped to the artist segment; two-significant-word
  requirement; title substring gated behind artist evidence.
- `tests/test_slskd_wrong_artist_rejection.py` — regression tests: the
  Orville Peck / The Fall of Troy case is rejected, single first-name
  tokens are not evidence, and legitimate matches (artist in filename or
  parent folder, multi-word artists) still pass.
