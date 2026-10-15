"""Link tasks to shared artifacts and store the observation a verification checked."""

from pathlib import Path

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "0009_task_references.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("ALTER TABLE verification_results DROP CONSTRAINT IF EXISTS verification_results_source_snapshot_sha256_check")
    op.execute("ALTER TABLE verification_results DROP CONSTRAINT IF EXISTS verification_results_observed_sha256_check")
    op.execute("ALTER TABLE verification_results DROP COLUMN IF EXISTS verifier_version")
    op.execute("ALTER TABLE verification_results DROP COLUMN IF EXISTS source_snapshot_sha256")
    op.execute("ALTER TABLE verification_results DROP COLUMN IF EXISTS observed_sha256")
    op.execute("DROP TABLE IF EXISTS task_artifacts")
