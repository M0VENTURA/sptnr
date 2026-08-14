"""Add user_favourites table for per-user star/love sync with Navidrome.

Each Navidrome user keeps their OWN favourite state (track / album / artist
hearts).  ``user_favourites`` stores that per-user state so one user's hearts
never apply to another user.  The active user is resolved from the app
session (``session["username"]``) / ``navidrome_users`` config.

Revision ID: 005_add_user_favourites
Revises: 004_add_folder_matches
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_add_user_favourites"
down_revision: Union[str, None] = "004_add_folder_matches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
    op.create_index("idx_user_favourites_user", "user_favourites", ["username", "entity_type"])


def downgrade() -> None:
    op.drop_index("idx_user_favourites_user", table_name="user_favourites")
    op.drop_table("user_favourites")
