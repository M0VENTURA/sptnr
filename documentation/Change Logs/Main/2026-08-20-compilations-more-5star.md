# Single-artist compilations (Greatest Hits) get more 5★ tracks

## Symptom

A Greatest Hits / Best-Of album — a curated collection of the artist's
genuine hits — was rated like a normal studio album and got FEWER 5★ tracks
than it should.  On a 20-track hits compilation, tracks ranked #4+ were
suppressed to 4★ even when they were genuinely the artist's biggest hits.

## Root cause

Two compilation-hostile paths in `_assign_stars` / `post_album_star_ratings`:

1. **The 5★ album-top-N rank used the compilation's own tracklist.**
   `qualifies_album = _album_rank(score, album_scores) <= album_top_n`
   ranked a track against the OTHER hits on the compilation.  Because every
   compilation track is a hit, only the top-3 of the compilation qualified
   for 5★ — every other genuine hit missed the bar.  (The 1-4★ base band
   already used the ARTIST reference; only the 5★ album-rank path used the
   wrong reference.)

2. **The era 5★ slot cap applied to compilations.**
   `max_5star_slots` (default peak-era 4) demoted surplus era-5★ tracks to
   4★ — capping a hits album at 4 regardless of how many real hits it holds.

## Fix

`services/popularity/stages/finalise_stage.py`:

1. **`_assign_stars`** — for single-artist compilations
   (`is_compilation=True`), the 5★ `qualifies_album` rank now uses
   `ref_scores` (the ARTIST catalogue) instead of `album_scores` (the
   compilation's own tracklist).  A track's rank reflects its standing in
   the artist's real catalogue (#1 hit stays #1), so every genuine hit on a
   Greatest Hits album can reach 5★.  True Various-Artists albums keep the
   album reference (their "artist" has no catalogue).

2. **`post_album_star_ratings`** — the era 5★ slot cap is skipped for
   single-artist compilations (`not is_compilation` guard).  A curated hits
   album legitimately holds many 5★ tracks; capping it at 4 would demote
   real #1s just because they share a hits tracklist.  True VA albums keep
   the cap.

## Files

- `services/popularity/stages/finalise_stage.py` — artist-reference
  `qualifies_album` for compilations + slot-cap skip.
- `tests/test_compilation_more_5star.py` — regression tests (hit ranked #4
  on the compilation still 5★ via artist rank; regular albums unchanged;
  many hits all stay 5★; VA albums keep the cap).
