"""Contract schema and runtime privileges."""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "schema.sql"


def upgrade() -> None:
    op.execute(SCHEMA.read_text(encoding="utf-8"))
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aether_api') THEN
            CREATE ROLE aether_api LOGIN NOSUPERUSER NOBYPASSRLS;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aether_worker') THEN
            CREATE ROLE aether_worker LOGIN NOSUPERUSER NOBYPASSRLS;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aether_verifier') THEN
            CREATE ROLE aether_verifier LOGIN NOSUPERUSER NOBYPASSRLS;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aether_dispatcher') THEN
            CREATE ROLE aether_dispatcher LOGIN NOSUPERUSER NOBYPASSRLS;
          END IF;
        END;
        $$;
        """
    )
    op.execute(SQL.joinpath("task_status_guard.sql").read_text(encoding="utf-8"))
    op.execute(SQL.joinpath("0002_privileges.sql").read_text(encoding="utf-8"))
    op.execute(SQL.joinpath("0003_grants.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE row record;
        BEGIN
          FOR row IN
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' AND tablename <> 'alembic_version'
          LOOP
            EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', row.tablename);
          END LOOP;
          DROP FUNCTION IF EXISTS public.reject_contract_update() CASCADE;
          DROP FUNCTION IF EXISTS public.aether_reject_api_task_projection() CASCADE;
          DROP FUNCTION IF EXISTS public.aether_owner_visible(uuid) CASCADE;
        END;
        $$;
        """
    )
