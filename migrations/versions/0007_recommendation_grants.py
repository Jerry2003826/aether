"""Let the API record a recommendation impression while ranking visible sources."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT ON profile_snapshots, recommendation_impressions TO aether_api")
    op.execute("GRANT UPDATE ON command_outbox TO aether_verifier")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON profile_snapshots, recommendation_impressions FROM aether_api")
    op.execute("REVOKE UPDATE ON command_outbox FROM aether_verifier")
