"""Adapt tables created by the previous MVP schema.

Revision ID: 0002_adapt_legacy_schema
Revises: 0001_initial
"""
from alembic import op

revision = "0002_adapt_legacy_schema"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'users' AND column_name = 'name'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'users' AND column_name = 'full_name'
            ) THEN
                ALTER TABLE users RENAME COLUMN name TO full_name;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'users' AND column_name = 'school'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'users' AND column_name = 'school_name'
            ) THEN
                ALTER TABLE users RENAME COLUMN school TO school_name;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'users' AND column_name = 'max_user_id'
                  AND data_type <> 'bigint'
            ) THEN
                ALTER TABLE users
                    ALTER COLUMN max_user_id TYPE BIGINT
                    USING max_user_id::BIGINT;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'users' AND column_name = 'role'
                  AND udt_name <> 'user_role'
            ) THEN
                ALTER TABLE users
                    ALTER COLUMN role TYPE user_role
                    USING role::user_role;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'users' AND column_name = 'status'
            ) THEN
                ALTER TABLE users ADD COLUMN status request_status;
                UPDATE users
                SET status = CASE
                    WHEN role IN ('teacher')
                        THEN 'pending'::request_status
                    ELSE 'approved'::request_status
                END;
                ALTER TABLE users
                    ALTER COLUMN status SET DEFAULT 'approved'::request_status,
                    ALTER COLUMN status SET NOT NULL;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'reminder_deliveries'
                  AND column_name = 'delivered_at'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'reminder_deliveries'
                  AND column_name = 'created_at'
            ) THEN
                ALTER TABLE reminder_deliveries
                    RENAME COLUMN delivered_at TO created_at;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "The legacy schema conversion is irreversible without risking user data."
    )
