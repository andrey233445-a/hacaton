"""Add users.ui_mode interface mode toggle (chat / web mini app).

Revision ID: 0006_users_ui_mode
Revises: 0005_users_notify_levels
"""
from alembic import op

revision = "0006_users_ui_mode"
down_revision = "0005_users_notify_levels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # asyncpg не поддерживает несколько команд в одном prepared statement.
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "ui_mode VARCHAR(16) NOT NULL DEFAULT 'chat'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS ui_mode")
