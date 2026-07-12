# scan_helpers split map

## New files

```text
services/scanning/bootstrap.py
services/scanning/cleanup.py
services/scanning/filters.py
services/scanning/navidrome_import.py
services/scanning/payload_builder.py
services/scanning/pipeline.py
services/scanning/scan_state.py
db/repositories/scan_repository.py
db/repositories/scan_cleanup_repository.py
```

## Shims included

The helpers/scan_*.py files are now compatibility shims so old imports still work while you migrate call sites.
```
