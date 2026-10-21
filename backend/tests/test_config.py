import pytest

from savetube.config import Settings

PG_DEV = (
    "postgres://savetube:p%40ss@pg-dev.lab.hiriri.com:5432/savetube"
    "?sslmode=verify-full&sslrootcert=/Users/me/.postgresql/postgres-dev-ca.crt"
)


def test_dsn_keeps_tls_parameters_for_sqlalchemy():
    s = Settings(database_url=PG_DEV)
    assert s.sqlalchemy_url == (
        "postgresql+psycopg://savetube:p%40ss@pg-dev.lab.hiriri.com:5432/savetube"
        "?sslmode=verify-full&sslrootcert=/Users/me/.postgresql/postgres-dev-ca.crt"
    )


def test_procrastinate_gets_the_dsn_unchanged():
    assert Settings(database_url=PG_DEV).conninfo == PG_DEV


def test_postgresql_scheme_is_accepted():
    s = Settings(database_url="postgresql://u@db:5432/x")
    assert s.sqlalchemy_url == "postgresql+psycopg://u@db:5432/x"


def test_the_password_stays_out_of_repr():
    assert "p%40ss" not in repr(Settings(database_url=PG_DEV))


def test_another_scheme_is_refused():
    with pytest.raises(ValueError):
        Settings(database_url="mysql://u@db/x")


def test_log_level_defaults_to_info_and_reads_any_case():
    assert Settings().log_level == "INFO"
    assert Settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValueError):
        Settings(log_level="loud")
