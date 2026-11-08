"""homework points

Revision ID: 0007_homework_points
Revises: 0006_users_ui_mode
Create Date: 2026-10-28

Учитель задаёт стоимость домашки в баллах; за сдачу ученик получает именно
это количество очков (раньше было зашито 10).
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_homework_points"
down_revision = "0006_users_ui_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = [c["name"] for c in insp.get_columns("homework")]
    if "points" not in cols:
        op.add_column(
            "homework",
            sa.Column("points", sa.Integer(), nullable=False, server_default="10"),
        )


def downgrade() -> None:
    op.drop_column("homework", "points")