"""Add user_favourites table for per-user star/love sync with Navidrome.

Each Navidrome user keeps their OWN favourite state (track / album / artist
hearts).  ``user_favourites`` stores that per-user state so one user's hearts
never apply to another user.  The active user is resolved from the app
session (``session["username"]``) / ``navidrome_users`` config.

Idempotent against the runtime schema bootstrap, which creates this same
table from ``db.schema.TABLES_TO_ENSURE`` — a bare ``CREATE TABLE`` aborted
the chain with ``relation already exists`` on bootstrap-built databases.

Revision ID: 005_add_user_favourites
Revises: 004_add_folder_matches
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from migrations.idempotent import create_index_if_missing, create_table_if_missing

revision: str = "005_add_user_favourites"
down_revision: Union[str, None] = "004_add_folder_matches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    def _create() -> None:
        op.create_table(
            "user_favourites",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.Text(), nullable=False),
            sa.Column("entity_type", sa.Text(), nullable=False),
            sa.Column("entity_id", sa.Text(), nullable=False),
            sa.Column("navidrome_id", sa.Text(), nullable=True),
            sa.Column("is_favourite", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("username", "entity_type", "entity_id", name="uq_user_favourites_entity"),
        )

    created = create_table_if_missing(bind, "user_favourites", _create)

    if created:
        op.create_index(
            "idx_user_favourites_user",
            "user_favourites",
            ["username", "entity_type"],
        )
    else:
        create_index_if_missing(
            bind,
            "idx_user_favourites_user",
            "user_favourites",
            "CREATE INDEX idx_user_favourites_user ON user_favourites (username, entity_type)",
        )


def downgrade() -> None:
    op.drop_index("idx_user_favourites_user", table_name="user_favourites")
    op.drop_table("user_favourites")