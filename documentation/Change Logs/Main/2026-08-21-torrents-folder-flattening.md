# Matched Folders: flatten torrents root into per-album entries

## What changed

The monitor page's **Matched Folders** section treated the torrents root as
a single folder, merging every album under it into one entry:

- The old skip was case-sensitive (`entry == "torrents"`), so a `Torrents` /
  `TORRENTS` folder (common with qBittorrent / deluge / transmission)
  passed through as ONE folder.
- `_get_files_in_folder` recurses (depth 3), so all album subfolders' audio
  files were collected into that single entry.
- `_derive_folder_group` saw multiple artists/albums → fell back to
  `group_key = "Torrents"` (the root name) → the UI showed one merged
  "Torrents" folder, and matching it associated/confirmed the **entire
  `/Torrents` directory** instead of a single album.

The fix flattens the torrents root (any casing) into **one Matched Folder
per album subfolder**, so each album gets its own Match / Change Match /
Confirm Match / Delete and per-track actions.

## Implementation

`services/downloads/download_folder_service.py`:

- New `_is_torrents_root(name)` — case-insensitive torrents-root check.
- New `_iter_matched_folder_candidates(downloads_dir, archive_dir)` — yields
  `(folder_abs, display_name)` per Matched Folder: one per top-level folder,
  except the torrents root which is flattened into its album subfolders.
  Hidden/dunder dirs and the FLAC conversion archive are pruned at every
  level.
- `get_unmatched_folders()` now iterates the candidate helper (instead of the
  old case-sensitive inline skip), so the torrent albums get individual
  entries.
- `auto_delete_imported_folders()` uses the same helper, so fully-imported
  torrent album subfolders are auto-deleted individually — never the whole
  torrents root.

## Files

- `services/downloads/download_folder_service.py`
- `tests/test_torrents_folder_flattening.py` (new)

## Tests

`tests/test_torrents_folder_flattening.py` covers: per-album entries under a
capital-`Torrents` root (no merged root entry), per-album `group_key`
isolation, scoped `audio_count`, hidden/archive pruning, and lowercase
`torrents` also surfacing its album subfolders.
