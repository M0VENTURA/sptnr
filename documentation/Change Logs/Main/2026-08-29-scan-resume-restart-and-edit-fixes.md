# Scan resume/restart semantics + edit-track/disc/genre fixes (2026-08-29)

## 1. Scan resume / restart / forced semantics

**Requested behaviour:**
- **Resume by default** — running a scan continues from the last checkpoint.
- **Restart option** — start from the beginning.
- **Forced (no restart)** — resume from the last scan IN FORCED MODE.
- **Forced + restart** — start from the beginning in forced mode.
- **Restart without forced** — start from the top, still skipping recently
  scanned items (change detection / skip windows apply).

**Changes (`services/scanning/pipeline.py`):**
- `start_library_scan(resume, force, restart)` — clears the checkpoint only
  when `restart` (or `not resume`); a forced scan WITHOUT restart keeps the
  checkpoint so it resumes in forced mode.
- `_validated_resume_artist(artists, checkpoint_path, force, restart)` —
  loads the checkpoint regardless of `force` (forced scans now RESUME);
  `restart=True` always returns None (start from the top); stale checkpoints
  are still cleared + logged.
- `run_full_library_scan(force, restart)` — passes restart through.

**`routes/scan_routes/control.py` `/scan/start`:**
- New `restart` form field (checkbox). `force` alone = forced RESUME;
  `restart` = restart; both = forced restart. Flash shows the mode.

**Dashboard popularity scan (`/api/popularity/run`, `dashboard.html`,
`dashboard.js`, `routes/schemas.py`):**
- `ScanRequest` gained `restart: bool`.
- The route resolves `resume_from` from the checkpoint (per mode:
  `full_scan` for "all", `popularity_scan` otherwise); `restart` clears it.
- Dashboard selector gained a **Restart** checkbox; `runDashboardPopularityScan`
  passes `restart` through.
- `_run_full_scan_as_artist_pipeline` (the "All" scan) now saves a checkpoint
  per artist and clears it ONLY on completion, so a stopped/failed "All" scan
  resumes where it left off.

**UI (`templates/components/_scan_selector.html`):** the shared scan selector
now includes a Restart checkbox next to the scan-type dropdown.

## 2. Genre consolidation + similar-artist matching

- `routes/ui_routes.py` `collect_album_genres` now runs every candidate genre
  through the genre-aggregation normalisation (`normalize_genre` synonyms,
  admin/junk filtering) and dedupes on a punctuation-stripped key — the album
  page no longer shows "Hip Hop" + "Hip-Hop" / "R&B" + "RnB" duplicates.
- `services/metadata/artist_metadata_service.py`:
  - New `_norm_artist_key()` — case/punctuation/"The"-prefix tolerant key.
  - `_annotate_similar_artist` now marks `in_collection` via the normalised
    key, so similar artists already in the library ("Beatles, The" vs
    "The Beatles") are correctly filtered from the discovery list.

## 3. Album disc number not clearing ("keeps the 1")

- `routes/ui_routes.py` album save: when `album_disctotal` is EMPTY, the
  handler now infers multi-disc from the actual track disc numbers.  If no
  track is on a disc > 1 (e.g. one track has "1", the rest empty) the stray
  disc numbers are STRIPPED from every track (DB + file tags) — the reported
  "one track has disc 1, the rest are empty, it's keeping the 1" bug.  Real
  multi-disc evidence (any disc > 1) still keeps/defaults disc numbers.

## 4. Edit track modal save on album page

- **Root cause (flags):** `_normalize_track_updates` coerced BIGINT flag
  columns (`is_cover`, `is_live`, `is_remix`, `alternate_take`,
  `is_compilation`) through `_coerce_optional_int`, which did
  `str(True) → "True" → not a digit → None`.  The album page's modal sends
  checkboxes as JS booleans, so every flag save silently NULLed the flags.
- `routes/track_routes.py` `_coerce_optional_int` now maps JSON booleans to
  1/0 (True → 1, False → 0) before the digit check.
- **Duplicate DOM ids:** the simple edit modal and the comprehensive edit
  modal both used `id="editTrackId"`.  The simple modal's hidden field is now
  `simpleEditTrackId` and `album_detail.js` updated, so
  `getElementById('editTrackId')` unambiguously targets the comprehensive
  modal.
- `templates/components/modals/_track_edit.html` gained `editTrackComposerField`
  and `editTrackCommentField` (referenced by the downloads-queue page's inline
  modal JS — its modal would otherwise crash on open).

## Files

- `services/scanning/pipeline.py`
- `routes/scan_routes/control.py`
- `routes/scan_routes/api.py`
- `routes/schemas.py`
- `services/scanning/pipelines/popularity_pipeline.py`
- `templates/components/_scan_selector.html`
- `templates/pages/dashboard.html`
- `static/js/dashboard.js`
- `routes/ui_routes.py`
- `services/metadata/artist_metadata_service.py`
- `routes/track_routes.py`
- `templates/components/modals/_track_edit.html`
- `static/js/album_detail.js`
- Tests: `tests/test_scan_continuation_guard.py`,
  `tests/test_album_disc_number_strip.py`,
  `tests/test_track_edit_and_disc_genre_fixes.py` (new)
