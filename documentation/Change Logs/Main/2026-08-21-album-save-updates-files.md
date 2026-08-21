# Album save updates files (and surfaces when it can't)

## Symptom

On the album page, matching an album to a MusicBrainz release and pressing
Save reported "Album metadata saved — N track(s) updated", but the audio
files (and Navidrome after a scan) still showed the OLD data.  Only the
database was being updated.

## Root causes

1. **Path resolver missed the configured music root.**  The stored
   ``file_path`` for Navidrome-imported tracks is the RELATIVE Navidrome
   path (``Artist/Album/01 - Track.mp3``).  ``_resolve_music_file_path``
   only tried the legacy env vars (``MUSIC_FOLDER``/``MUSIC_ROOT``/
   ``MUSIC_DIR``) and ``/music`` — it never consulted the CONFIG-driven
   root (``music.root`` / ``music_root`` / ``navidrome.music_folder`` from
   ``config.yaml`` via ``resolve_music_dir``).  When the library lives
   outside ``/music`` and no env var is set, resolution returned ``None``
   and the file write was silently skipped (DB-only update).

2. **The album page swallowed file-write failures.**  The per-track file
   sync caught exceptions at ``debug`` level and never told the user that
   only the DB changed.

3. **The release picker never set the Album Artist field.**  ``populateAlbumFields``
   populated title / year / type / MBIDs / cover art but NOT ``album_artist``
   — so a release whose primary artist differs from the stored one kept the
   old artist on save.

## Fix

`services/metadata/tag_file_service.py`:

- ``_resolve_music_file_path`` now prepends the CONFIG-driven root
  (``resolve_music_dir``) to the candidate roots BEFORE the env vars and
  ``/music``, so relative Navidrome paths resolve against the library root
  the app actually uses.

`routes/ui_routes.py` (album POST):

- Track file-write success explicitly; failures are counted and surfaced as
  a warning flash ("updated in the database but NOT in the audio files…")
  and logged at ``warning`` with the resolved path (or ``None``) so the
  cause is visible.

`static/js/album_detail.js`:

- ``populateAlbumFields`` accepts an ``albumArtist`` param and sets the
  ``album_artist`` form field, so a release-picker selection (including the
  primary-credit artist) is actually saved.  The lookup callback and the
  release picker thread the primary album artist through.

## Files

- `services/metadata/tag_file_service.py`
- `routes/ui_routes.py`
- `static/js/album_detail.js`

## Tests

Existing resolver tests (`test_track_sync_and_live_detection.py`) still pass;
the fanout tests pass apart from one pre-existing `db_session` decorator
failure unrelated to this change.
