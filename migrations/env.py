from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
import os

# Import your Base
from app.models.database import Base  # adjust path if needed

# Alembic Config object
config = context.config

# ✅ Get DB URL from environment
url = os.getenv("DATABASE_URL")
if not url:
    raise ValueError("DATABASE_URL is not set. Make sure it’s available in your environment.")
config.set_main_option("sqlalchemy.url", url)
if url:
    config.set_main_option("sqlalchemy.url", url)

# Logging setup
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata (for autogenerate)
target_metadata = Base.metadata

def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
