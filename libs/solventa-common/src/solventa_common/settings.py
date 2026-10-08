"""Configuración base de los servicios.

Lee el contrato de variables de entorno que entrega infra/apps (ecs.tf) en AWS
y mantiene valores locales por defecto para desarrollo:

    SERVICE_NAME, ENVIRONMENT, CORRELATION_HEADER, REQUEST_ID_HEADER
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SSLMODE, DB_POOL_SIZE

Las variables propias de cada servicio usan el prefijo SOLVENTA_. Para la base,
la prioridad es: SOLVENTA_DATABASE_URL > DB_* (AWS) > Postgres local.
"""

from urllib.parse import quote

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env(name: str) -> AliasChoices:
    # Acepta el nombre que entrega la infraestructura y su variante SOLVENTA_.
    return AliasChoices(name, f"SOLVENTA_{name}")


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOLVENTA_", extra="ignore")

    service_name: str = "servicio"
    environment: str = Field("local", validation_alias=_env("ENVIRONMENT"))
    correlation_header: str = Field("X-Correlation-Id", validation_alias=_env("CORRELATION_HEADER"))
    request_id_header: str = Field("X-Request-Id", validation_alias=_env("REQUEST_ID_HEADER"))

    db_host: str | None = Field(None, validation_alias=_env("DB_HOST"))
    db_port: int = Field(5432, validation_alias=_env("DB_PORT"))
    db_name: str | None = Field(None, validation_alias=_env("DB_NAME"))
    db_user: str = Field("solventa", validation_alias=_env("DB_USER"))
    db_password: str = Field("solventa", validation_alias=_env("DB_PASSWORD"), repr=False)
    db_sslmode: str = Field("prefer", validation_alias=_env("DB_SSLMODE"))
    db_pool_size: int = Field(5, validation_alias=_env("DB_POOL_SIZE"))
    database_url_override: str | None = Field(None, validation_alias="SOLVENTA_DATABASE_URL", repr=False)

    @property
    def database_name(self) -> str:
        # En AWS cada servicio tiene su base (api-socios -> api_socios); igual en local.
        return self.db_name or self.service_name.replace("-", "_")

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        user = quote(self.db_user, safe="")
        password = quote(self.db_password, safe="")
        host = self.db_host or "localhost"
        return (
            f"postgresql://{user}:{password}@{host}:{self.db_port}/{self.database_name}"
            f"?sslmode={self.db_sslmode}"
        )
