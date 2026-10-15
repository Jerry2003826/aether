"""Let the worker advance a project's visibility epoch when material is forgotten."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT UPDATE (visibility_epoch) ON projects TO aether_worker")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (visibility_epoch) ON projects FROM aether_worker")
