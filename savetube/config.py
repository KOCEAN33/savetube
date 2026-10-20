from functools import cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SCHEMES = ("postgres://", "postgresql://")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SAVETUBE_", env_file=".env", extra="ignore")

    # A libpq URL. Locally the shared dev server, pg-dev.lab.hiriri.com, with
    # sslmode=verify-full. In the cluster the PgBouncer Pooler.
    database_url: SecretStr = SecretStr("postgres://savetube@127.0.0.1:5432/savetube")

    archive_root: Path = Path("/archive")
    static_dir: Path | None = None
    # Mounted read-only from a Secret. Every run copies it into a writable
    # directory first, because yt-dlp writes the cookie jar back.
    cookies_file: Path | None = None

    # The browser-facing origin. Redirect URIs and the cookie's Secure flag come
    # from here, never from request headers.
    public_url: str = "http://127.0.0.1:8000"
    session_secret: SecretStr = SecretStr("")
    session_max_age: int = 7 * 24 * 3600

    oidc_issuer: str = "https://auth.hiriri.com"
    oidc_client_id: str = ""
    # Unset means a public client with PKCE only.
    oidc_client_secret: SecretStr | None = None
    # A value of the Zitadel `groups` claim. Unset admits any user the issuer
    # lets through.
    oidc_required_group: str | None = None

    metrics_port: int = 9090

    namespace: str = "default"
    recorder_cronjob: str = "savetube-recorder"

    @field_validator("database_url")
    @classmethod
    def _libpq_url(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith(_SCHEMES):
            raise ValueError("SAVETUBE_DATABASE_URL must start with postgres:// or postgresql://")
        return v

    @property
    def sqlalchemy_url(self) -> str:
        _, rest = self.database_url.get_secret_value().split("://", 1)
        return f"postgresql+psycopg://{rest}"

    @property
    def conninfo(self) -> str:
        """The libpq URL as given, for procrastinate."""
        return self.database_url.get_secret_value()

    @property
    def incoming_root(self) -> Path:
        return self.archive_root / ".incoming"


@cache
def get_settings() -> Settings:
    return Settings()
