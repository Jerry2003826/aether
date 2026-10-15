"""Nullable project note. Existing rows stay in place."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS display_note text")


def downgrade() -> None:
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS display_note")
