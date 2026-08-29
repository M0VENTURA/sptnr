# Matched Folders: band folders flattened + completion "no local file found" loop fixed (2026-08-29)

Two reported issues from today's logs, both fixed on `develop`:

## 1. Matched Folders still merging everything under /torrents

**Symptom:** a band folder like
`Ignea - (ex - Parallax)` under `/torrents` rendered as ONE Matched-Folder
entry with every album's audio merged into it (58 audio files across 6+
albums), and matching/deleting it touched the whole directory.

**Root cause:** the candidate iterator had two separate flattening
implementations that disagreed:
- the **torrents branch** (`_collect_torrent_albums`) recursed and early-
  returned on the first folder with DIRECT audio — a band folder with a
  stray track swallowed its album subfolders;
- the **non-torrents branch** walked with a different depth rule, so a
  top-level band folder could still surface as one merged entry;
- `_iter_torrent_album_candidates` (used by Refresh Matches) only returned
  albums ONE level under the torrents root — band-level rows were never
  migrated down to the albums, and `_resolve_folder_match` only inherited
  the torrents-ROOT row (never a band row).

**Fix (`services/downloads/download_folder_service.py`):**
- New `_collect_album_folders(folder_abs, max_depth)` — ONE recursion used
  everywhere: every folder holding audio DIRECTLY is its own candidate;
  cd/disc subfolders belong to their parent; a folder with direct audio that
  ALSO holds album subfolders yields BOTH entries (nothing silently merged);
  hidden/dunder/archive pruned at every level.
- `_iter_matched_folder_candidates` + `_iter_torrent_album_candidates` both
  delegate to it, so the torrents root, band folders and top-level folders
  flatten identically and Refresh Matches migrates to the same rows the UI
  shows.
- `_resolve_folder_match` now inherits a BAND-level (any ancestor) row for
  torrent-root descendants, not just the root row.
- `refresh_folder_matches` migrates any row above the album level (torrents
  root OR band folder) down to one row per album subfolder.
- **Merge guard** — `_assert_single_album_folder` refuses
  `associate_folder_to_release`, `match_folder_to_release`,
  `delete_download_folder`, `delete_folder_track` and
  `move_folder_track_to_library` on the torrents root itself or any folder
  that contains non-disc audio-bearing subfolders ("This folder contains
  multiple album subfolders — match each album separately").

## 2. "slskd transfer succeeded but no local file found — retrying" loop

**Symptom (Aephanemer/Utopie):** every queue cycle the completion service
reported a succeeded transfer with no local file, failed + requeued the
item, the retry's search downloaded a DIFFERENT file (08 → 18 → 13 …),
which also "succeeded" without appearing → infinite re-download loop.

**Root causes + fix (`services/downloads/download_completion_service.py`):**
1. `_reconcile_transfer_state` cancelled + failed a succeeded-but-unfound
   transfer IMMEDIATELY (the file is often still flushing/renaming).  It now
   logs once, keeps the item `downloading` (returns `False`) and only fails
   it after a 15-minute grace period (`_SLSKD_SUCCEEDED_NOT_FOUND_TIMEOUT_
   MINUTES`), cancelling the transfer + blocking the peer at that point.
2. `check_completed_downloads` processed STALE snapshot rows — a retry had
   already requeued + re-downloaded the item, but the old row (with the OLD
   `found_filename`) was failed AGAIN → double-fail.  Each item is now
   re-fetched fresh first via `_refresh_downloading_item`; an item that is
   no longer `downloading` is skipped.
3. `_wait_for_transfer_file` only walked 3 levels deep — `music/Artist/Album`
   is depth 3 but the FILE lives one level deeper, so it was never found
   when the direct remote-join path drifted.  The walk is now deep (8
   levels), prunes hidden/dunder/archive dirs, and also searches the
   torrents dir + sibling `torrents` roots (slskd's complete directory can
   differ from the app's downloads dir).  The same extra roots were added to
   the `slskd_completed` map-building basename walk in `check_completed_
   downloads`.

## Files

- `services/downloads/download_folder_service.py`
- `services/downloads/download_completion_service.py`
- `tests/test_torrent_band_folder_never_merged.py` (new)
- `tests/test_download_completion_not_found_loop.py` (new)
- `tests/test_peer_failure_handling.py` (success-but-unfound behaviour now
  asserts the grace period instead of immediate fail+cancel)

## Tests

Covered: per-album candidates under a band folder (never the band folder
itself), scoped audio counts, stray-track folder yields both entries,
band-level association inherited by albums, merge guard (root + band refused,
real album still deletable), guard fires before any MB call,
`refresh_folder_matches` migrates band-level rows; deep basename search with
path drift, sibling torrents-root search, succeeded-but-unfound stays
`downloading` inside the grace window then fails after it, and
`_refresh_downloading_item` skipping requeued items.
