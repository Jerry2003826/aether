"""Allow a model request to record a reported cost above its reservation."""

from pathlib import Path

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "0011_model_call_lifecycle.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("ALTER TABLE model_requests DROP CONSTRAINT IF EXISTS model_requests_usage_status_check")
    op.execute("ALTER TABLE model_requests DROP CONSTRAINT IF EXISTS model_requests_known_fields_check")
    op.execute("ALTER TABLE model_requests DROP CONSTRAINT IF EXISTS model_requests_unknown_cost_check")
    op.execute(
        "ALTER TABLE model_requests ADD CONSTRAINT model_requests_usage_status_check "
        "CHECK (usage_status IN ('reserved', 'known', 'unknown', 'scripted', 'cancelled_before_dispatch'))"
    )
    op.execute(
        "ALTER TABLE model_requests ADD CONSTRAINT model_requests_known_fields_check "
        "CHECK (usage_status <> 'known' OR (actual_micro_usd IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE model_requests ADD CONSTRAINT model_requests_unknown_cost_check "
        "CHECK (usage_status <> 'unknown' OR actual_micro_usd IS NULL)"
    )
