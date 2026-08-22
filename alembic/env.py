"""Alembic migrations environment for Akashic Agent.

Uses a synchronous psycopg2 connection (not asyncpg) because Alembic
does not natively support async engines. This is fine — migrations are
a one-time boot-time concern.

For production, set the DATABASE_URL env var to override the value
in alembic.ini, e.g.:

    DATABASE_URL=postgresql://user:pass@host:5432/nexus alembic upgrade head
"""

import os
import re
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config object
config = context.config

# Override DB URL from env var if present
db_url = os.environ.get("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

# Set up Python loggers from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Import all models so Alembic autogenerate can detect them ──
from bootstrap.db.models.base import Base  # noqa: E402
from bootstrap.db.models.tenant import TenantModel  # noqa: E402, F401
from bootstrap.db.models.session import SessionModel, MessageModel  # noqa: E402, F401
from bootstrap.db.models.memory import (  # noqa: E402, F401
    ConsolidationEventModel,
    MemoryItemModel,
    MemoryReplacementModel,
)
from bootstrap.db.models.proactive import (  # noqa: E402, F401
    ContextOnlyTimestampModel,
    DeliveryModel,
    SessionStateModel,
    TickLogModel,
    TickStepLogModel,
)
from bootstrap.db.models.rachael import (  # noqa: E402, F401
    RachaelActivationEventModel,
    RachaelEdgeModel,
    RachaelEmbeddingCacheModel,
    RachaelMigrationRunModel,
    RachaelNodeModel,
    RachaelQueryLogModel,
    RachaelSalienceStateModel,
    RachaelSourceSessionSnapshotModel,
)
from bootstrap.db.models.extras import AppConfigModel, ScheduledJobModel  # noqa: E402, F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without a DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
