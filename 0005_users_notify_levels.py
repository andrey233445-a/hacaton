"""Add users.notify_7d/notify_3d/notify_1d deadline reminder toggles.

Revision ID: 0005_users_notify_levels
Revises: 0004_users_mood_survey_hour
"""
from alembic import op

revision = "0005_users_notify_levels"
down_revision = "0004_users_mood_survey_hour"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # asyncpg не поддерживает несколько команд в одном prepared statement —
    # каждый ALTER выполняем отдельным op.execute.
    for column in ("notify_7d", "notify_3d", "notify_1d"):
        op.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} BOOLEAN")
        op.execute(f"UPDATE users SET {column} = TRUE WHERE {column} IS NULL")
        op.execute(f"ALTER TABLE users ALTER COLUMN {column} SET DEFAULT TRUE")
        op.execute(f"ALTER TABLE users ALTER COLUMN {column} SET NOT NULL")


def downgrade() -> None:
    for column in ("notify_7d", "notify_3d", "notify_1d"):
        op.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {column}")
