"""Add a database default for legacy users.created_at.

Revision ID: 0003_users_created_at_default
Revises: 0002_adapt_legacy_schema
"""
from alembic import op
from sqlalchemy import DateTime, func

revision = "0003_users_created_at_default"
down_revision = "0002_adapt_legacy_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE users SET created_at = now() WHERE created_at IS NULL")
    op.alter_column(
        "users",
        "created_at",
        existing_type=DateTime(),
        server_default=func.now(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "created_at",
        existing_type=DateTime(),
        server_default=None,
        nullable=False,
    )
