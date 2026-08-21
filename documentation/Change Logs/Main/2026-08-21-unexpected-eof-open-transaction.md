# Fix "unexpected EOF on client connection with an open transaction"

## Symptom

PostgreSQL logged repeatedly:

```
LOG:  unexpected EOF on client connection with an open transaction
```

during normal operation (interleaved with routine 30 s checkpoints).

## Root cause

The queue cycle / scheduler tick cross-process locks
(`services/queue/queue_lock.py::_pg_advisory_lock`) acquired a Postgres
advisory lock with `SELECT pg_try_advisory_lock(hashtext(:key))` and then
held that session — still inside an open transaction — for the ENTIRE
critical section:

- The queue batch (`process_cycle`) performs Soulseek searches, MusicBrainz
  calls, filesystem moves and metadata reads, easily exceeding 60 s.
- `db/utils.py` sets `idle_in_transaction_session_timeout=60000` (60 s) on
  every new connection.
- After 60 s idle-in-transaction, Postgres terminates the connection,
  logging `unexpected EOF on client connection with an open transaction` —
  dropping the advisory lock and any work running under it.

PostgreSQL advisory locks are SESSION-scoped, not transaction-scoped: they
survive `COMMIT` and are only released by `pg_advisory_unlock` or session
close.  The fix is therefore to COMMIT after acquiring (and after unlocking)
so the session is never idle-in-transaction while holding the lock.

## Second regression: "you don't own a lock of type ExclusiveLock"

The first attempt at the commit fix used a POOLED SQLAlchemy session.  After
the commit, the pool could recycle / re-checkout the connection, so the
`pg_advisory_unlock` ran on a DIFFERENT connection than the one that took the
lock — Postgres logged `you don't own a lock of type ExclusiveLock` and the
lock leaked.

`_pg_advisory_lock` now uses a DEDICATED raw psycopg2 connection
(`db.utils.get_db_connection`), which is not pooled: lock and unlock always
hit the SAME connection, the session-scoped lock survives the post-acquire
commit, and a killed worker releases the lock on connection close.

## Fix

`services/queue/queue_lock.py`:

- `_pg_advisory_lock` now opens a raw, non-pooled connection, runs
  `pg_try_advisory_lock` through a cursor, COMMITS immediately after
  acquiring (so `idle_in_transaction_session_timeout` never kills the
  connection mid-batch), and runs `pg_advisory_unlock` + COMMIT on the SAME
  connection before closing.
- Tolerates both `RealDictCursor` rows (`row["acquired"]`) and plain tuples
  (`row[0]`).

`db/engine.py`:

- Both the sync and async PostgreSQL engines now set `pool_recycle=50`
  (overridable via `DB_POOL_RECYCLE_SECONDS`) so pooled connections are
  recycled before the 60 s idle-in-transaction timeout can kill a connection
  that sat idle inside an open transaction (the same EOF pattern on pooled
  connections; `pool_pre_ping` only masks a dead checkout, it does not stop
  Postgres logging the EOF).

## Files

- `services/queue/queue_lock.py`
- `db/engine.py`
- `tests/test_queue_lock_transaction.py` (new)

## Tests

`tests/test_queue_lock_transaction.py` covers: the lock connection is
committed after acquiring the advisory lock (and after unlocking) while still
holding the session-scoped lock, and a busy lock skips both commit and
unlock.
