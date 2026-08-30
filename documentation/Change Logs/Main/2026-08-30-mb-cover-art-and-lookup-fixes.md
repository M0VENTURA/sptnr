# MusicBrainz cover art + lookup + metadata-checklist fixes (2026-08-30)

## 1. Cover art not updating on MusicBrainz lookup

**Root cause:** ``apply_mbid_to_album`` wrote the MB album/release-group ID
to the DB + file tags, but the ``cover_url`` was only stored as a STRING on
the track row — the actual image was never downloaded from Cover Art Archive
and never embedded into the audio files.

**Fix (`services/metadata/album_service.py`):**
- ``apply_mbid_to_album`` now downloads the cover from Cover Art Archive
  (release MBID first, then release-group MBID), saves it to the ``album_art``
  table, and embeds it into every track file (``cover_art_data`` + mime).
  Returns ``cover_art_applied``.

**Album save route (`routes/ui_routes.py`):** when the album form carries a
``cover_art_url`` (from an MB lookup), the Save handler now downloads + embeds
the image into all track files and stores it in ``album_art`` (with a
"🎨 Album cover art downloaded and embedded" flash).

## 2. Discogs / MusicBrainz art search returning nothing

- **Discogs:** ``search_album_art_external`` hardcoded ``token=""`` so the
  Discogs source ALWAYS returned "No album art found".  It now reads the
  configured ``api_integrations.discogs.token``.
- **MusicBrainz:** ``fetch_album_art_from_musicbrainz`` now prefers a
  CONCRETE release's Cover Art Archive front image (browse the group's
  releases first) before falling back to the release-group front image —
  per-release art is populated far more often.

## 3. Upload image from file returns an error

- ``set_album_art_from_upload`` / ``set_album_art_from_url`` now embed the
  art into the album's track files AND return ``files_updated`` (the JS
  already expected it).  Embedding failures are non-fatal (the art is still
  saved in the DB).
- ``apply_album_art_to_tracks`` hardened: resolves relative Navidrome paths
  via ``resolve_music_file_path`` and tolerates legacy tuple rows.

## 4. MB lookup "only the artist field seems to be used"

**Root cause:** the strict ``artist:"X" AND releasegroup:"Y"`` query returns 0
when the local album title has edition markers / spelling drift, and the
fallback dropped to ``artist:"X"`` returning ALL the artist's releases.

**Fix (`routes/musicbrainz_routes.py`):** the artist-only fallback now filters
release-groups by fuzzy album-title similarity (with substring/edition-marker
bonus) and sorts by similarity, so the album relevance is preserved.

## 5. MB lookup doesn't update album metadata / cover / composers

**Fix (`services/enrichment/musicbrainz_service.py` + `static/js/album_detail.js`):**
- ``compare_musicbrainz_release`` now carries FULL per-track MB enrichment in
  each comparison entry: ``mb_writer``, ``mb_composer``, ``mb_lyricist``,
  ``mb_is_cover``, ``mb_original_cover_artist``, ``mb_musicbrainz_genres``,
  ``mb_work_mbid``, plus album-level ``mb_release_mbid`` / ``mb_album_artist_mbid``.
- "Update All Tracks" now sends composer/lyricist/writer/genres/cover/work
  AND the album-level fields (album title/artist/year/release MBID/RG MBID/
  album-artist MBID) to every track, then fetches + embeds the album cover art
  via Cover Art Archive.
- ``/api/track/update-metadata`` gained ``original_cover_artist`` in its
  allowed fields.

## 6. Metadata checklist coverage (confirmed + completed)

MusicBrainz lookup + metadata phase now captures:
- **Core grouping:** `musicbrainz_albumid` ✓, `musicbrainz_artistid` ✓,
  `musicbrainz_albumartistid` ✓ (new: from the primary credit),
  `musicbrainz_releasegroupid` ✓.
- **Library structure:** `albumartist` ✓, `compilation` ✓ (new: from release-
  group secondary types), `originaldate`/`original_year` ✓ (new: from
  first-release-date so remasters sort chronologically).
- **Covers / works:** `musicbrainz_workid` ✓, `iswc` ✓ (new: from the work),
  `original_cover_artist` ✓, `original_title` ✓ (new: the work title when a
  cover), `composer` ✓ (new), `lyricist` ✓ (new), `writer` ✓.
- **Multi-disc:** each track carries BOTH the medium-specific `track_number`
  AND an `absolute_track_number` (sequential across discs) so locally-numbered
  1..22 libraries match without disc tags; duration is always present for the
  5s-delta verification.

New DB columns (migration `012_add_tracks_mb_composer_lyricist_iswc`):
``iswc``, ``lyricist``, ``original_title`` (+ schema registry + tag writers
FLAC/MP3 TXXX).

## Files

- `services/metadata/album_service.py`
- `services/enrichment/album_art_service.py`
- `services/enrichment/musicbrainz_service.py`
- `routes/musicbrainz_routes.py`
- `routes/ui_routes.py`
- `routes/track_routes.py`
- `services/metadata/tag_file_service.py`
- `db/schema.py`
- `migrations/versions/012_add_tracks_mb_composer_lyricist_iswc.py` (new)
- `static/js/album_detail.js`
- `tests/test_mb_cover_art_and_lookup_fixes.py` (new)
