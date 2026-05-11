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
# Status display configuration — single source of truth for UI presentation.
#
# Each entry maps a queue status string to its Bootstrap badge CSS class,
# Bootstrap Icons icon name, and a human-readable label.  Templates can look
# up ``STATUS_DISPLAY_CONFIG[status]`` to render a consistent badge without
# duplicating if/elif chains.  The ``@app.context_processor`` in app.py
# injects this dict into every Jinja template as ``queue_status_config``.
# JavaScript equivalents in downloads.html consume it via the injected
# ``queueStatusConfig`` template variable.
# ---------------------------------------------------------------------------
STATUS_DISPLAY_CONFIG: dict[str, dict[str, str]] = {
    'queued': {
        'label': 'Queued',
        'css': 'bg-warning text-dark',
        'icon': 'clock',
    },
    'searching': {
        'label': 'Searching',
        'css': 'bg-warning text-dark',
        'icon': 'search',
    },
    'downloading': {
        'label': 'Downloading',
        'css': 'bg-primary',
        'icon': 'download',
    },
    'completed': {
        'label': 'Completed',
        'css': 'bg-success',
        'icon': 'check-circle',
    },
    'failed': {
        'label': 'Failed',
        'css': 'bg-danger',
        'icon': 'x-circle',
    },
    'unmatched': {
        'label': 'Unmatched',
        'css': 'bg-warning text-dark',
        'icon': 'exclamation-triangle',
    },
    'moving': {
        'label': 'Moving',
        'css': 'bg-info text-dark',
        'icon': 'arrow-right-circle',
    },
    'queried': {
        'label': 'Queried',
        'css': 'bg-secondary',
        'icon': 'question-circle',
    },
    'copy_recommended': {
        'label': 'Copy Recommended',
        'css': 'bg-info text-dark',
        'icon': 'files',
    },
    'possible_duplicate': {
        'label': 'Possible Duplicate',
        'css': 'bg-secondary',
        'icon': 'copy',
    },
    'imported': {
        'label': 'Imported',
        'css': 'bg-success',
        'icon': 'check2-all',
    },
    'awaiting_selection': {
        'label': 'Select File',
        'css': 'bg-primary',
        'icon': 'hand-index',
    },
    'in_collection': {
        'label': 'In Collection',
        'css': 'bg-success',
        'icon': 'collection',
    },
    'matched': {
        'label': 'Matched',
        'css': 'bg-info text-dark',
        'icon': 'check-circle',
    },
}

# Fallback entry used when a status value is not found in STATUS_DISPLAY_CONFIG.
_STATUS_DISPLAY_FALLBACK: dict[str, str] = {
    'label': 'Unknown',
    'css': 'bg-secondary',
    'icon': 'question',
}


def get_status_display(status: str) -> dict[str, str]:
    """Return the display config for *status*, falling back to a generic entry."""
    return STATUS_DISPLAY_CONFIG.get(status, {**_STATUS_DISPLAY_FALLBACK, 'label': status or 'Unknown'})

# ---------------------------------------------------------------------------
# Soulseek / slskd candidate-scoring constants
# ---------------------------------------------------------------------------

# Track-variant qualifier words used by Soulseek candidate scoring and
# post-download metadata matching.  Single source of truth shared by
# queue_processor.py and download_queue_manager.py.
TITLE_VARIANT_TOKENS = frozenset([
    "acoustic", "demo", "edit", "instrumental", "intro", "live", "mix",
    "orchestral", "radio", "remaster", "remastered", "remix", "version",
])

# "Soft" variant tokens may be absent from file tags for the correct
# recording (e.g. "radio edit", "edited version", "single version").  A
# mismatch on these alone is allowed when the file duration closely
# confirms the expected duration (≤2 s).  All other variant tokens
# ("live", "acoustic", "orchestral", "remix", etc.) indicate genuinely
# different recordings and are enforced strictly in both directions.
SOFT_VARIANT_TOKENS = frozenset(["version", "edit", "radio"])

# Hard duration tolerance (seconds) used across candidate scoring and
# post-download verification.  When the expected duration is unknown the
# caller skips the check entirely; this value only applies when a
# duration reference is available.
SLSKD_DURATION_TOLERANCE_SECONDS = 5


def _is_live_track_from_genre(genre_value):
    """Return True when *genre_value* indicates a live recording.

    Checks the raw genre string (which may be backslash-separated in ID3)
    for the word ``live`` as a standalone token.  This lets the local-file
    matcher reject a studio queue item against a track whose tags say
    ``Genre: Live`` even when the title tag omits the ``(Live)`` suffix.
    """
    if not genre_value:
        return False
    # Handle backslash-separated multi-genre strings (ID3 TCON style)
    parts = str(genre_value).replace("/", "\\").split("\\")
    for part in parts:
        if part.strip().lower() == "live":
            return True
    return False
