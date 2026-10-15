"""Let the API append task events and record owner approval decisions."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT ON task_events TO aether_api")
    op.execute("GRANT UPDATE ON approvals TO aether_api")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON task_events FROM aether_api")
    op.execute("REVOKE UPDATE ON approvals FROM aether_api")
