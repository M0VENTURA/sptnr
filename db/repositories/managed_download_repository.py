"""Managed download repository.

Provides DB queries for the managed-download tracking table.
Tracks release downloads by their release ID and status progression.

Responsibilities:
- ``get_managed_download`` – Lookup a download by its local ID.
- ``update_download_status`` – Update a download's status and optional method.
"""

from sqlalchemy import text

from db.engine import db_session


def get_managed_download(download_id: int):
    with db_session() as session:
        result = session.execute(
            text("SELECT release_id, ... FROM managed_downloads WHERE id = :id"),
            {"id": download_id},
        )
        return result.fetchone()


def update_download_status(download_id: int, status: str, method: str | None = None):
    with db_session() as session:
        if method:
            session.execute(
                text("UPDATE managed_downloads SET status = :status, method = :method WHERE id = :id"),
                {"status": status, "method": method, "id": download_id},
            )
        else:
            session.execute(
                text("UPDATE managed_downloads SET status = :status WHERE id = :id"),
                {"status": status, "id": download_id},
            )