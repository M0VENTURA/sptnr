# Universal search speed + self-titled album separation

## 1. Universal search was slow

### Symptom

The universal search (global modal, fires on every keystroke with a 50 ms
debounce) became noticeably slow recently.

### Root cause

`POST /api/search` runs three queries (artists, albums, tracks) over the
whole `tracks` table.  Each WHERE clause uses
`LOWER(COALESCE(col, '')) LIKE '%query%'` (contains) across multiple
columns with `OR`.  Plain btree indexes cannot serve a leading-`%`
`LIKE`, so every keystroke triggered full sequential scans of the table —
once per bucket.

### Fix

- `db/schema.py`: enable `pg_trgm` (`CREATE EXTENSION IF NOT EXISTS
  pg_trgm`) and add GIN trigram indexes on the EXACT expressions the search
  queries use (`lower(coalesce(title,''))`, `lower(coalesce(artist,''))`,
  `lower(coalesce(album_artist,''))`, `lower(coalesce(album,''))` with
  `gin_trgm_ops`), so contains searches are index-backed.
- `db/bootstrap.py`: `_ensure_index` tolerates a missing `pg_trgm`
  extension (CREATE EXTENSION may need superuser) — the index is skipped
  with a warning instead of failing the schema bootstrap.
- The index build runs once at startup (`CREATE INDEX IF NOT EXISTS`); after
  that the universal search no longer scans the table.

## 2. Self-titled albums merged (Weezer × 3)

### Symptom

Weezer have three different albums called "Weezer" (1994 Blue, 2001 Green,
2008 Red).  On the artist page they all merged into ONE row, and clicking it
went to an album page that merged all three albums' tracks.

### Root cause

- The artist page grouped albums by `album_name.lower()` only — no year —
  so the three "Weezer" albums shared one key.
- The album page route matched tracks by
  `album_artist + album` only, with no way to tell the three apart.

### Fix

- **Artist page** (`routes/ui_routes.py`): library albums now group by
  `album_name::year` (and the "appears on" compilation list by
  `artist::album::year`), so each self-titled album is its own row with its
  own year and tracklist.  Accordion ids in
  `_album_category_section.html` include the year so the collapse targets
  don't collide.
- **Album page route**: `/album/<artist>/<album>/<year>` — an optional
  third path segment filters the track query to tracks whose leading
  4-digit year matches, so `/album/Weezer/Weezer/1994` shows only the 1994
  album.  A non-year or out-of-range segment is ignored (no empty page); a
  year with no matching tracks keeps all tracks (stale/unknown year).
- **Links** now include the year when known: artist page discography rows
  and track album links, "appears on" rows, the shared album category
  section, the dashboard's recently-added albums (its query now selects the
  album year), and the unified search modal's local/owned album links.
- **Album page title** shows the year parenthetically when known.

## Files

- `db/schema.py`
- `db/bootstrap.py`
- `routes/ui_routes.py`
- `templates/components/_album_category_section.html`
- `templates/pages/artist_detail.html`
- `templates/pages/album_detail.html`
- `templates/pages/dashboard.html`
- `static/js/unified_search.js`

## Notes

The trgm indexes require `pg_trgm`; if the DB user cannot create the
extension, the bootstrap skips the GIN indexes with a warning and the
search falls back to the previous (correct but slower) behaviour.
