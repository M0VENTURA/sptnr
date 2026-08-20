# Single detection: check corroborating sources after a primary confirm

## Symptom

The single-detection source table / `Matched:` log line only showed sources
checked BEFORE the pipeline gave up.  For a track confirmed by MusicBrainz
(e.g. "Master of the Universe" — `Discogs: ✖, MB: ✓, LF: ✓` →
`Matched: musicbrainz, lastfm`), the Discogs-video and ISRC checks never
ran: after a primary (Discogs or MusicBrainz) confirm, both were gated off,
so a track that is BOTH an MB single AND has a Discogs video only ever
showed the MB match.

## Root cause — two early-stop gates

1. **ISRC lookup** ran only when `not musicbrainz_confirmed`.  A track can
   be a single on one release while its recording's release-groups on other
   releases (or its ISRC's canonical recording) carry different primary
   types — the release-group match the scan already made can miss a single
   that the ISRC path would confirm.
2. **Discogs-video corroboration** ran only when
   `not discogs_confirmed and not musicbrainz_confirmed`.  Each call is a
   rate-limited Discogs API hit (0.35s/req), so it was gated to weak-evidence
   tracks only — but that hides legitimate corroboration.

## Fix

`services/enrichment/single_detection_service.py`:

1. **ISRC lookup now runs for EVERY track with an ISRC** — even when MB
   already confirmed via its release-group.  The path is bounded (only
   tracks that carry an ISRC) and MB-cached, so it costs nothing for the
   majority of tracks and adds genuine cross-release corroboration where it
   matters.

2. **Discogs-video corroboration is configurable** —
   `single_detection.always_check_discogs_video` (default **False**).  By
   default it still runs only when neither Discogs nor MusicBrainz
   confirmed (preserving the rate-limit cost); set the flag to also run it
   after a primary confirm so a track that is both an MB single AND has a
   Discogs video shows the full corroboration in the source table.

The `check_high_confidence_dynamic` call was found to be vestigial (its
result is ignored) — no change needed there.

## Files

- `services/enrichment/single_detection_service.py` — ISRC gate relaxed,
  `_always_check_discogs_video()` helper, Discogs-video gate configurable.
- `templates/pages/config.html` — "Always Check Discogs Video" toggle.
- `static/js/config.js` — toggle saved under `single_detection`.
- `tests/test_single_detection_source_gates.py` — regression tests (ISRC
  runs after MB confirm; no ISRC → no lookup; Discogs-video default skip;
  Discogs-video enabled; config default false).
