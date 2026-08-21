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

## Fix

`services/queue/queue_lock.py`:

- `_pg_advisory_lock` now calls `session.commit()` immediately after
  `pg_try_advisory_lock` returns true (rollback on commit failure).  The
  session-scoped lock is unaffected; the transaction ends, so
  `idle_in_transaction_session_timeout` never kills the connection mid-batch.
- The same commit-on-exit is applied after `pg_advisory_unlock`.

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

`tests/test_queue_lock_transaction.py` covers: the lock session is committed
after acquiring the advisory lock (and after unlocking) while still holding
the session-scoped lock, and a busy lock skips both commit and unlock.
