# Full DB rebuild map

## New structure

```text
db/
├── __init__.py
├── bootstrap.py
├── cleanup.py
├── context.py
├── database.py
├── schema.py
├── schema_helpers.py
├── utils.py
└── repositories/
    ├── __init__.py
    ├── artists.py
    ├── bookmarks.py
    ├── genres.py
    ├── library.py
    ├── navidrome.py
    └── tracks.py
```

## Old helper file mapping

```text
helpers/db_utils.py   -> db/utils.py + db/schema_helpers.py + repositories
helpers/db_context.py -> db/context.py
helpers/db_cleanup.py -> db/cleanup.py + db/repositories/tracks.py
helpers/db_queries.py -> db/repositories/library.py + artists.py + bookmarks.py
helpers/check_db.py   -> db/bootstrap.py
```

## Preferred imports

```python
from db.context import db_cursor
from db.utils import get_db_connection, row_get
from db.bootstrap import init_database_and_schema, ensure_full_schema
from db.repositories.tracks import get_top_tracks, insert_or_update_track
from db.repositories.artists import insert_artist
from db.repositories.genres import aggregate_genres_from_tracks
```

## Duplicate functions merged

- `db_cursor` now lives only in `db/context.py`.
- `table_exists` and `get_table_columns` now live in `db/schema_helpers.py`.
- `delete_tracks_by_id` now lives in `db/repositories/tracks.py`.
- Artist collection queries now live in `db/repositories/artists.py`.
- Library summary queries now live in `db/repositories/library.py`.
- Genre functions now live in `db/repositories/genres.py`.
- Navidrome bulk upsert now lives in `db/repositories/navidrome.py`.
- `db/database.py` is now only a thin public facade.
```
