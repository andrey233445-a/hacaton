"""Add users.mood_survey_hour for the daily mood survey setting.

Revision ID: 0004_users_mood_survey_hour
Revises: 18e138fba0ce
"""
from alembic import op

revision = "0004_users_mood_survey_hour"
down_revision = "18e138fba0ce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS mood_survey_hour INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS mood_survey_hour")
