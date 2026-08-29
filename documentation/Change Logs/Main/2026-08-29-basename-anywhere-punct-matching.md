# Completion: basename-anywhere punctuation-tolerant matching (2026-08-29)

## Symptom

The Aephanemer "Utopie" download loop ran all day: every queue item
reported ``slskd succeeded but no local file found (will keep polling)``
with ``local_file_path='(empty)'``, stayed ``downloading`` for the grace
period, was failed + re-searched + re-downloaded — endlessly.  The files
genuinely land on disk, but the completion matcher never found them.

## Root cause

slskd does NOT expose a ``localFilePath`` in its transfer API — the parser
reads a field that does not exist, so ``local_file_path='(empty)'`` forever.
The on-disk path must be DERIVED from the remote filename.  The derivation
was failing because of **basename spelling drift between peers**:

- The queue's ``found_filename`` records the SELECTED peer's remote path:
  ``music/Aephanemer/[2025] Utopie/Aephanemer - Utopie - 15 - Contrepoint
  (instrumental).flac`` (U+2010 hyphens ``‐``, accented ``è``, parens).
- The file lands on disk with a DIFFERENT peer's spelling:
  ``.../Aephanemer - Utopie - 15 Contrepoint instrumental.flac`` (ASCII
  hyphens, no accents, no parens).

Exact, lowercase, and accent-stripped basename keys all failed because the
**hyphen characters, parentheses, and accents differ** — so the basename walk
reported "no local file found" even though the file was sitting right there.

## Fix

`services/downloads/download_completion_service.py`:

- New ``_punctuation_stripped_key(name)`` — lowercases, accent-strips, and
  removes EVERY non-alphanumeric character, so
  ``Par‐delà le mur des siècles (instrumental)`` and
  ``par-dela le mur des siecles instrumental`` collapse to the same key.
- ``_build_download_index`` now also keys each file by its punctuation-
  stripped basename (``by_punct``).
- ``check_completed_downloads``:
  - the fs walk now covers the downloads root PLUS the torrents dir and
    sibling ``torrents`` roots (the same area the scan walks), deduping
    paths;
  - the exact-match block appends ``by_punct`` candidates for the queue
    ``found_filename`` BASENAME — the file is found ANYWHERE under the
    downloads tree by name alone, ignoring the folder path (the user's
    requested behaviour);
  - the ``slskd_completed`` map-building basename walk also matches the
    punctuation key.
- ``_wait_for_transfer_file`` (grace-window poll) compares the punctuation
  key too, so a just-landed file with hyphen/paren drift is claimed the
  moment it appears.
- ``_fuzzy_candidates`` gains a punctuation-key fallback against the queue
  title, so a file whose basename matches the title after punctuation
  stripping is also surfaced.

## Files

- `services/downloads/download_completion_service.py`
- `tests/test_download_completion_not_found_loop.py` (new tests: punct-key
  collapse, hyphen-drift walk find, index punct lookup, fuzzy punct fallback)
