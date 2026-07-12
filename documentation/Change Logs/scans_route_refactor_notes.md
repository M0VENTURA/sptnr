# Scan route and pipeline refactor notes

## Key compatibility decision

`routes/scans.py` remains as a compatibility entrypoint:

```python
from routes.scans import scans_bp
```

Internally it now imports `scans_bp` from `routes.scan_routes`.

This avoids a breaking import change in your blueprint bootstrap.

## New route structure

```text
routes/scan_routes/
  __init__.py
  _common.py
  artist_album.py
  popularity.py
  essentia.py
  mp3_import.py
  control.py
  api.py

routes/navidrome/
  __init__.py
  playlists.py
  ratings.py
  scan.py
```

## New service structure

```text
services/scanning/pipelines/
  artist_pipeline.py
  album_pipeline.py
  navidrome_pipeline.py
  popularity_pipeline.py
  essentia_pipeline.py

services/scanning/runtime_state.py
services/scanning/progress.py
```

## What moved

- Album pipeline logic moved to `services/scanning/pipelines/album_pipeline.py`.
- Artist pipeline route calls now use `services/scanning/pipelines/artist_pipeline.py`.
- Navidrome import-only scan moved to `services/scanning/pipelines/navidrome_pipeline.py`.
- `/scan/navidrome` and `/scan/stop-navidrome` moved to `routes/navidrome/scan.py`.
- General stop/status/progress routes stay under scan routes.

## Important integration note

Replace your blueprint bootstrap import:

```python
from routes.navidrome_api import navidrome_bp
```

with:

```python
from routes.navidrome import navidrome_bp
```

The existing import remains safe for scans:

```python
from routes.scans import scans_bp
```

because `routes/scans.py` is now a compatibility shim.
