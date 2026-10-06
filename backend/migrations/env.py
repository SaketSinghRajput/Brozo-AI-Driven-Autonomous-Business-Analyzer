"""Alembic environment. Migrations are hand-written and run as the schema-owner role."""

import os

from alembic import context
from sqlalchemy import create_engine

config = context.config


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("APP_MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("Set APP_MIGRATION_DATABASE_URL (schema-owner role) to run migrations.")
    return url


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=None, transaction_per_migration=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; run against a database.")
run_migrations_online()
