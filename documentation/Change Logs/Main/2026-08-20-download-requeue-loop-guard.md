# Stop the download re-download loop when a matching file exists but is unconfirmed

## Symptom

Two queue items (Voice of Baceprot — "What's The Holy (Nobel) Today", Stray
Kids — "The Little Things") kept re-downloading indefinitely.  slskd appends
a `_<timestamp>` suffix to every re-download of the same name, so the
downloads folder accumulated **30+ copies** of each file:

```
/downloads/nicotine/Voice of Baceprot - What's The Holy (Nobel) Today_639226592128975884.flac
/downloads/nicotine/Voice of Baceprot - What's The Holy (Nobel) Today_639226606492436226.flac
... (33 total)
```

The files were on disk with the right names, but the completion service
never confirmed them — and each failed confirmation requeued the item, which
re-downloaded, which created another `_<timestamp>` copy.

## Root cause

`check_completed_downloads` matches a queue item against downloaded files in
three steps (slskd localFilePath → exact filename → fuzzy metadata/filename).
When all three fail to CONFIRM the file (slskd returns an empty
`localFilePath`; the file's embedded metadata is missing/undetermined and
the strict fuzzy gate requires metadata OR artist confirmation), the item
falls to step 4: "no file found and stale in downloading" →
`mark_failed("No file found while marked downloading")` → requeue →
re-download.  The file was never deleted (it isn't a metadata MISMATCH, just
unconfirmed), so every retry piled up another numbered copy.

## Fix

`download_completion_service.check_completed_downloads` step 4 now checks
whether a matching-named file EXISTS on disk before requeueing:

- `_matching_file_exists_unconfirmed(item, fs_files, downloads_dir)` — a
  new helper that returns the first file whose basename shares most of the
  queue item's title tokens (the `_<timestamp>` duplicate suffix is stripped
  by the tokenizer).  If such a file exists but could not be auto-confirmed,
  the item is surfaced for **manual review** instead of being requeued and
  re-downloaded: the file stays in the downloads folder, the item stays out
  of the auto-download loop, and a `manual_review` queue event + unified-log
  line explains why.

This stops the infinite re-download copy accumulation while keeping the
existing behavior for genuinely-missing downloads (no matching file on disk
→ still requeues normally).

## Files

- `services/downloads/download_completion_service.py` — `_matching_file_exists_unconfirmed`
  helper + stale-requeue guard in `check_completed_downloads`.
- `tests/test_download_completion_unmatched_artist.py` — regression tests
  (duplicate-suffix file detected, matching file detected, unrelated file
  returns None, apostrophe title detected).
