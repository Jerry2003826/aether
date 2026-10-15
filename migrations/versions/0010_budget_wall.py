"""Record the wall-clock limit on a task budget account."""

from pathlib import Path

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "0010_budget_wall.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS capability_grants_token_lookup ON capability_grants")
    op.execute("ALTER TABLE budget_accounts DROP COLUMN IF EXISTS active_seconds")
