# Popularity pipeline bootstrap notes

## What changed

The root `popularity.py` should no longer contain the scanner implementation.
It is now a thin CLI entrypoint that calls:

```python
from services.popularity.pipeline import run_popularity_scan
```

`app.py` now passes the Flask application object into:

```python
initialize_app_services(app)
```

`helpers/task_manager.py` now accepts that app object and passes it into
background workers that need a Flask app context.

## New files

```text
app.py
helpers/task_manager.py
popularity.py
routes/popularity_routes.py
services/popularity/pipeline.py
services/popularity/legacy_scanner.py
documentation/popularity_pipeline_bootstrap_notes.md
```

## Required migration step

Move the full old root-level `popularity.py` implementation into:

```text
services/popularity/legacy_scanner.py
```

Then replace root `popularity.py` with the thin entrypoint from this package.

During migration, `services/popularity/pipeline.py` delegates to:

```text
services.popularity.legacy_scanner.popularity_scan
```

You can override this with:

```text
POPULARITY_LEGACY_MODULE=some.module.name
```

as long as that module exposes:

```python
popularity_scan(...)
```

## Config for background popularity scheduler

Add this to `config.yaml` if you want automatic popularity scans:

```yaml
features:
  popularity_scheduler:
    enabled: false
    interval_seconds: 3600
    run_on_startup: false
    force: false
    artist_filter:
    album_filter:
```

It defaults to disabled.

## Route registration

`routes/popularity_routes.py` defines:

```python
popularity_bp
```

If your `register_all_blueprints(app)` does not auto-discover route modules,
add this inside your app bootstrap:

```python
from routes.popularity_routes import popularity_bp
app.register_blueprint(popularity_bp)
```

## Why this structure

- `app.py` stays orchestration-only.
- `helpers/task_manager.py` owns background worker startup.
- `routes/` owns manual trigger/status endpoints.
- `services/popularity/pipeline.py` is the stable pipeline entrypoint.
- `services/enrichment/single_detection_service.py` owns single detection.
