from alembic import context
from sqlalchemy import create_engine, pool

from savetube.db import PSYCOPG_CONNECT_ARGS
from savetube.db.models import Base

config = context.config
target_metadata = Base.metadata


def include_name(name, type_, parent_names) -> bool:
    # procrastinate owns its tables. Without this, autogenerate drops them.
    return not (type_ == "table" and name.startswith("procrastinate_"))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        include_name=include_name,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
        connect_args=PSYCOPG_CONNECT_ARGS,
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_name=include_name
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
