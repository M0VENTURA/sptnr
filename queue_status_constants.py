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
    # 'moving' is the atomic in-flight state used by _try_claim_for_move().
    # Including it here ensures add_to_queue() treats a mid-move item as an
    # existing duplicate and will not create a second queue entry for it.
    'moving',
)
ACTIVE_QUEUE_STATUS_SQL = ", ".join(f"'{s}'" for s in ACTIVE_QUEUE_STATUSES)

# In-flight processing statuses: the narrower subset used for "is this
# album/artist currently being fetched?" style queries.
PROCESSING_STATUSES = ('queued', 'searching', 'downloading')
PROCESSING_STATUS_SQL = ", ".join(f"'{s}'" for s in PROCESSING_STATUSES)

# ---------------------------------------------------------------------------
# Soulseek / slskd candidate-scoring constants
# ---------------------------------------------------------------------------

# Track-variant qualifier words used by Soulseek candidate scoring and
# post-download metadata matching.  Single source of truth shared by
# queue_processor.py and download_queue_manager.py.
TITLE_VARIANT_TOKENS = frozenset({
    "acoustic", "demo", "edit", "instrumental", "intro", "live", "mix",
    "orchestral", "radio", "remaster", "remastered", "remix", "version",
})

# "Soft" variant tokens may be absent from file tags for the correct
# recording (e.g. "radio edit", "edited version", "single version").  A
# mismatch on these alone is allowed when the file duration closely
# confirms the expected duration (≤2 s).  All other variant tokens
# ("live", "acoustic", "orchestral", "remix", etc.) indicate genuinely
# different recordings and are enforced strictly in both directions.
SOFT_VARIANT_TOKENS = frozenset({"version", "edit", "radio"})

# Hard duration tolerance (seconds) used across candidate scoring and
# post-download verification.  When the expected duration is unknown the
# caller skips the check entirely; this value only applies when a
# duration reference is available.
SLSKD_DURATION_TOLERANCE_SECONDS = 5
