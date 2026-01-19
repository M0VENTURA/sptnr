# Deprecated Files

This folder contains deprecated files that are no longer used in the active codebase but are preserved for historical reference.

## Files

### sptnr.py

**Status**: Deprecated (No longer used)

**Reason**: This was the original CLI rating tool. All functionality has been migrated to the modern architecture:
- Popularity scoring → `popularity.py`
- Singles detection → `single_detector.py`
- Web interface → `app.py`, `server.py`
- Configuration → `config.yaml` (via `config_loader.py`)

**Modern Alternatives**:
- For popularity scoring and star ratings: Use `popularity.py`
- For singles detection: Use `single_detector.py`
- For web interface: Use `app.py` or `server.py`
- For configuration: Use `config.yaml` via `config_loader.py`

See [MIGRATION_GUIDE.md](../MIGRATION_GUIDE.md) for details on the transition from `.env` to `config.yaml`.

**Do NOT use** this file. It remains here only for historical reference.
