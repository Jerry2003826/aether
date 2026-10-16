"""Separate logical artifacts from shared content-addressed blobs."""

from pathlib import Path

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "0012_artifact_identity.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS artifacts_live_identity")
    op.execute(
        "ALTER TABLE artifacts ADD CONSTRAINT artifacts_project_id_storage_key_key UNIQUE (project_id, storage_key)"
    )
