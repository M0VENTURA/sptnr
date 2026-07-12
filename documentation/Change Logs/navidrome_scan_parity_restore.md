# Navidrome scan parity restore

This patch restores the detailed behaviour from the original large
`routes/scans.py` Navidrome section while keeping the cleaner split:

```text
routes/navidrome/scan.py
services/scanning/pipelines/navidrome_pipeline.py
```

## Behaviour restored

- `mode=all|force|missing|resume|resume_force`
- `restart=1` clears checkpoint on first cycle
- `force_start=1` bypasses in-memory duplicate scan check
- checkpoint file: `navidrome_scan_checkpoint.json`
- progress file: `navidrome_scan_progress.json`
- resume uses `scan_resume.get_last_scanned_artist(scan_type="navidrome")`
- checkpoint fallback is used when not restarting
- matching resume artist is rescanned from its index
- `SPTNR_SKIP_SINGLES=1` is set during import-only scans and cleared in finally
- stop request is honoured between artists
- `features.perpetual` loops after a successful scan cycle
- subsequent perpetual cycles start from the beginning
- post-import hooks are called after successful cycles

## Files to replace

```text
routes/navidrome/scan.py
services/scanning/pipelines/navidrome_pipeline.py
```

Keep your existing blueprint package structure.
