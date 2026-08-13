"""Managed download repository — REMOVED.

Superseded by the Soulseek download_queue pipeline (services/downloads/*).
The original implementation queried a ``managed_downloads`` table that was
never created in the live schema, and the placeholder SQL ("SELECT
release_id, ...") could never execute. No code imports this module — it is
kept as an inert stub so stray imports fail loudly instead of running SQL
against a non-existent table.
"""
