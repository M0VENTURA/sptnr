# Matched Folders: re-associate torrent-root matches to album subfolders

## What changed

The torrents-root flattening (2026-08-21) turned the single merged
`/Torrents` Matched-Folder entry into one entry per album subfolder.  Folders
that were already **Matched** before that change kept their stored
`folder_matches` row pointing at the **root** (`/downloads/Torrents`), so
after the flattening the album subfolders rendered as **unmatched** again —
the association was invisible to them, and re-matching each album would have
duplicated the same MusicBrainz release per album.

Two fixes:

1. **Read-side inheritance** — `get_unmatched_folders()` now resolves each
   album's association through `_resolve_folder_match()`, which falls back to
   the torrent root's row when the subfolder has none.  Albums under a
   matched torrent root immediately show `Matched` / `[Change Match]`
   `[Confirm Match]` again (a subfolder with its own association always
   wins).
2. **Refresh Matches button** — a new `POST /api/downloads/folder-matches/
   refresh` endpoint migrates stale root-level rows down to one row per album
   subfolder (and deletes the root row).  The Matched Folders toolbar has a
   **Refresh Matches** button that runs it, so the association is persisted
   per-album and survives regardless of the read-side fallback.

## Implementation

`services/downloads/download_folder_service.py`:

- New `_iter_torrent_album_candidates(downloads_dir, archive_dir)` — yields
  `(album_abs, album_name)` per album directly under the torrents root (any
  casing), pruning hidden/dunder dirs and the conversion archive.
- New `_resolve_folder_match(folder_abs, *, match_rows)` — exact row first,
  then the parent torrent-root row as a fallback.
- `get_unmatched_folders()` — builds the `folder_matches` lookup once and
  applies `_resolve_folder_match` per entry (replacing the old exact-match
  merge loop); `status` is `matched` when either every audio file was
  imported OR a stored association resolves.
- New `refresh_folder_matches()` — for every association whose path is a
  torrent root, upserts one row per album subfolder with the same release
  metadata, then deletes the root row.

`routes/downloads.py`:

- New `POST /api/downloads/folder-matches/refresh` (offloaded to a thread).

`static/js/monitor.js` + `templates/pages/downloads/monitor.html`:

- **Refresh Matches** button in the Matched Folders toolbar; on success it
  re-renders the folder list and toasts how many album folders were
  re-associated.

## Files

- `services/downloads/download_folder_service.py`
- `routes/downloads.py`
- `static/js/monitor.js`
- `templates/pages/downloads/monitor.html`
- `tests/test_torrent_match_rematch.py` (new)

## Tests

`tests/test_torrent_match_rematch.py` covers: root association inherited by
each album subfolder (status + `release_mbid` + `match`), a subfolder's own
association winning over the root fallback, no association staying
unmatched, `refresh_folder_matches` migrating a root row to per-album rows
and deleting the root row, idempotency when there are no root rows, and
lowercase `torrents` roots.
