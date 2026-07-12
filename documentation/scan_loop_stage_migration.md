# Scan loop stage migration

This delta moves the popularity scan loop boundary into explicit stage files.
It does not invent provider/scoring logic. Move your current loop blocks into:

```text
services/popularity/stages/load_stage.py
services/popularity/stages/album_stage.py
services/popularity/stages/track_stage.py
services/popularity/stages/finalise_stage.py
```

The hook point is already active:

```python
album_context, track_contexts = prepare_tracks_for_album(...)
```

Use:

```python
track["lookup_title"]
track["lastfm_title"]
track["exclude_from_stats"]
```

instead of recomputing title cleanup and stat eligibility inside the loop.

