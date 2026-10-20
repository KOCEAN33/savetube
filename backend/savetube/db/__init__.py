from functools import cache

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from savetube.config import get_settings

# The address is PgBouncer in transaction mode. Server-side prepared statements
# do not survive a backend switch between transactions, so psycopg never
# prepares (prepare_threshold=None).
PSYCOPG_CONNECT_ARGS = {"prepare_threshold": None}


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, connect_args=PSYCOPG_CONNECT_ARGS, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


@cache
def default_sessionmaker() -> async_sessionmaker:
    """For the worker, scheduler and recorder processes. The web app owns its engine."""
    return make_sessionmaker(make_engine(get_settings().sqlalchemy_url))
