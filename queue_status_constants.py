"""
queue_status_constants.py
-------------------------
Single source of truth for download queue status constant sets.

Import from this module wherever status strings are needed to avoid
duplication and ensure all code stays in sync when statuses change.
"""

# All statuses that represent an item still "in play" (not terminal).
# Used for deduplication checks and active-queue reads.
ACTIVE_QUEUE_STATUSES = (
    'queued',
    'searching',
    'downloading',
    'unmatched',
    'queried',
    'copy_recommended',
    'moving',
)
ACTIVE_QUEUE_STATUS_SQL = ", ".join(f"'{s}'" for s in ACTIVE_QUEUE_STATUSES)

# In-flight processing statuses: the narrower subset used for "is this
# album/artist currently being fetched?" style queries.
PROCESSING_STATUSES = ('queued', 'searching', 'downloading')
PROCESSING_STATUS_SQL = ", ".join(f"'{s}'" for s in PROCESSING_STATUSES)
