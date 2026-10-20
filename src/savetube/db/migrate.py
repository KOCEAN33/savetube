from alembic import command
from alembic.config import Config
from procrastinate import SyncPsycopgConnector
from procrastinate.schema import SchemaManager

from savetube.config import Settings
from savetube.db import PSYCOPG_CONNECT_ARGS


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "savetube:migrations")
    # Alembic hands this to ConfigParser, which reads `%` as interpolation.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _procrastinate_installed(connector: SyncPsycopgConnector) -> bool:
    row = connector.execute_query_one(
        "SELECT to_regclass('procrastinate_jobs') IS NOT NULL AS present"
    )
    return bool(row["present"])


def migrate(settings: Settings) -> None:
    """Install procrastinate's schema on an empty database, then run alembic.

    procrastinate ships its own SQL migrations. A procrastinate upgrade that adds
    files under `SchemaManager.get_migrations_path()` needs an alembic revision
    applying them; this function only covers the first install.
    """
    connector = SyncPsycopgConnector(conninfo=settings.conninfo, kwargs=PSYCOPG_CONNECT_ARGS)
    connector.open()
    try:
        if not _procrastinate_installed(connector):
            SchemaManager(connector).apply_schema()
    finally:
        connector.close()
    command.upgrade(alembic_config(settings.sqlalchemy_url), "head")
