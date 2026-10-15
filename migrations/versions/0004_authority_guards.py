"""Reapply least-privilege grants and the terminal-task status guard."""

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute(SQL.joinpath("task_status_guard.sql").read_text(encoding="utf-8"))
    op.execute(SQL.joinpath("0003_grants.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    pass
