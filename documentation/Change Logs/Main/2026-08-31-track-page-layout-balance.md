# Track page layout: balanced 50/50 columns (2026-08-31)

## Symptom

On the track page (desktop), the sections didn't align with the hero/Play
button: "Play Track fills up window, Track information fills ~60% and Track
Scoring etc fills ~30%."  Track Scoring / Single Detection / Essentia Audio
Analysis looked cramped and didn't match the left-hand sections.

## Root cause

The two-column row used `col-12 col-lg-7` (left: Track Info / Advanced /
Additional Credits / Genres) and `col-12 col-lg-5` (right: Scoring / Single /
Essentia) — a 58/42 split.  The right column's inner grids used
`col-md-4` (3-across) and `col-md-3`/`col-md-5` controls, which were badly
cramped inside a 42%-width column, making the sections look broken and
inconsistent.

Additionally there was a breakpoint mismatch: the columns split at `lg`
(992px) while the mobile tab bar hides at `lg` too, but the split used
`col-lg-7/5`, so mid-range desktops saw everything stacked full-width with
no mobile tabs.

## Fix (`templates/pages/track_detail.html`)

- Both columns changed to **`col-12 col-lg-6`** — an equal 50/50 split at
  the same `lg` (992px) breakpoint where the mobile tab bar gives way to
  the desktop side-by-side layout.
- Track Scoring "Source Metrics" grids changed from `col-md-4` →
  `col-lg-4` (3-across only once the right column is at its full 50% width;
  stacks 1-up/2-up below).

## Files

- `templates/pages/track_detail.html`
