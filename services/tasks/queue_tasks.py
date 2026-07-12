"""Queue-specific async tasks.

Defines async task functions for queue operations that can be run
in the background via the central task manager.

Key Functions:
    - start_organize_group(): Start an async organise-group task for
      post-download file organisation.

Architecture:
    Thin wrappers around ``services.tasks.task_manager.run_async_task``
    for specific queue workflow operations.
"""

from services.tasks.task_manager import run_async_task
from services.queue.queue_processing_service import organize_group_sync


def start_organize_group(group_id, metadata):
    return run_async_task(
        "organize_group",
        organize_group_sync,
        group_id,
        metadata,
    )