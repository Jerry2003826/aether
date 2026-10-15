"""Grant privileges on tables created by later migrations."""

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = Path(__file__).resolve().parents[1].joinpath("sql", "0003_grants.sql").read_text(encoding="utf-8")
    op.execute(statements)


def downgrade() -> None:
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM aether_api, aether_worker;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM aether_verifier, aether_dispatcher;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM aether_api, aether_worker, aether_verifier;
        """
    )
