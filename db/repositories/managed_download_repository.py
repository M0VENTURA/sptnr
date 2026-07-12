"""Managed download repository.

Provides DB queries for the managed-download tracking table.
Tracks release downloads by their release ID and status progression.

Responsibilities:
- ``get_managed_download`` – Lookup a download by its local ID.
- ``update_download_status`` – Update a download's status and optional method.
"""

from db.context import db_cursor


def get_managed_download(download_id: int):
    with db_cursor() as (_conn, cursor):
        cursor.execute("SELECT release_id, ... FROM managed_downloads WHERE id = %s", (download_id,))
        return cursor.fetchone()

def update_download_status(download_id: int, status: str, method: str | None = None):
    with db_cursor(commit=True) as (_conn, cursor):
        if method:
            cursor.execute(
                "UPDATE managed_downloads SET status = %s, method = %s WHERE id = %s",
                (status, method, download_id)
            )
        else:
            cursor.execute(
                "UPDATE managed_downloads SET status = %s WHERE id = %s",
                (status, download_id)
            )