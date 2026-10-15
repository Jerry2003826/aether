from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
target = os.environ.get("AETHER_ALEMBIC_URL")
if target:
    config.set_main_option("sqlalchemy.url", target.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        autocommit = connection.execution_options(isolation_level="AUTOCOMMIT")
        context.configure(connection=autocommit, transaction_per_migration=False)
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
