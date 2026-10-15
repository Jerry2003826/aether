"""Allow restored commands to leave the dispatcher without being marked failed."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE command_outbox DROP CONSTRAINT IF EXISTS command_outbox_status_check")
    op.execute(
        """
        ALTER TABLE command_outbox ADD CONSTRAINT command_outbox_status_check
          CHECK (status IN ('PENDING', 'DELIVERING', 'DELIVERED', 'FAILED', 'STOPPED'))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE command_outbox SET status = 'FAILED' WHERE status = 'STOPPED'")
    op.execute("ALTER TABLE command_outbox DROP CONSTRAINT IF EXISTS command_outbox_status_check")
    op.execute(
        """
        ALTER TABLE command_outbox ADD CONSTRAINT command_outbox_status_check
          CHECK (status IN ('PENDING', 'DELIVERING', 'DELIVERED', 'FAILED'))
        """
    )
