"""Queue Configuration - slskd Timeouts and Retry Settings

This module centralizes all timeout and retry configuration for the slskd download queue.

Configuration Source:
    All values are loaded from config.yaml via helpers.config_helpers.get_slskd_timeouts()
    
    Example config.yaml:
    ```yaml
    slskd:
      timeouts:
        min_retry_delay_minutes: 60
        long_retry_delay_minutes: 1440
        remotely_queued_timeout_minutes: 60
        active_state_timeout_minutes: 240
        inter_item_delay_seconds: 5
        state_timeouts:
          "Requested": 30
          "Queued, Remotely": 60
          ...
    ```

Timeout Strategy:
    The system uses progressive timeouts based on transfer state:
    
    - Early states (Requested, Queued): Shorter timeouts (30-60 min)
    - Middle states (Initializing): Medium timeouts (120 min)
    - Late states (Downloading, InProgress): Longer timeouts (240 min)
    
    This prevents stuck transfers from blocking the queue indefinitely while
    allowing adequate time for slow transfers to complete.

Retry Strategy:
    - MIN_RETRY_DELAY_MINUTES: Floor between retries (prevents churn)
    - _SLSKD_LONG_RETRY_DELAY_MINUTES: For unmatched/duration mismatch failures
    - Failed items are retried the next day, not immediately
    
Usage:
    >>> from services.queue.queue_config import MIN_RETRY_DELAY_MINUTES
    >>> from services.queue.queue_config import _SLSKD_ACTIVE_STATE_TIMEOUTS
    >>> print(f"Retry delay: {MIN_RETRY_DELAY_MINUTES} minutes")

Architecture:
    Loaded once at module initialization from centralized config.
    Used by: queue_processing_service.py, queue_cleanup_service.py
"""

# Import centralized configuration getter
from helpers.config_helpers import get_slskd_timeouts

# Load configuration at module initialization
_timeouts = get_slskd_timeouts()

# Enforce a floor between retries so unavailable tracks do not churn.
MIN_RETRY_DELAY_MINUTES = _timeouts["min_retry_delay_minutes"]

# Retry delay (minutes) used for tracks that couldn't be matched today —
# "no results" and duration-mismatch failures both use this value so that the
# same track is not hammered on every run.
_SLSKD_LONG_RETRY_DELAY_MINUTES = _timeouts["long_retry_delay_minutes"]

# Remotely-queued transfers that stay in "Queued, Remotely" for longer than
# this are cancelled and re-queued so the item can be searched again.
_SLSKD_REMOTELY_QUEUED_TIMEOUT_MINUTES = _timeouts["remotely_queued_timeout_minutes"]

# General fallback timeout (minutes) for any active slskd transfer that is
# stuck in a non-terminal state without making progress.
_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES = _timeouts["active_state_timeout_minutes"]

# Per-state timeout map (minutes) for active slskd transfers.  When a transfer
# has been in one of these states for longer than the mapped value it is
# cancelled and the queue item is retried.
_SLSKD_ACTIVE_STATE_TIMEOUTS = _timeouts["state_timeouts"]

# Seconds to sleep between queue items in process_queue().  A short pause
# gives slskd breathing room to handle its own network duties (e.g. responding
# to distributed search requests from other peers) so that internal
# GetUserEndPoint timeouts are less likely to cascade.
_SLSKD_INTER_ITEM_DELAY_SECONDS = _timeouts["inter_item_delay_seconds"]