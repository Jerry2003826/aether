"""Apply the runtime patches bootstrap always ran: tokens_held, resource_heads and their policy and grants."""

from pathlib import Path

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "0005_runtime_patches.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Bootstrap-built databases at 0011 or 0012 already had these objects and that code reads them.
    pass
