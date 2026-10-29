from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # La infraestructura entrega ALLY_PAGOS_URL, _TIMEOUT_MS y _CREDENTIALS (infra/apps/ecs.tf).
    # La variante con el prefijo SOLVENTA_ es la convención de los demás servicios (por
    # ejemplo, para ejecutar en local).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class CredencialesAliado(BaseModel):
    """JSON {client_id, client_secret} de Secrets Manager (infra/platform/security.tf)."""

    # hide_input_in_errors: un JSON incompleto no debe mostrar el secreto al fallar el arranque.
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    client_id: SecretStr
    client_secret: SecretStr


# Solo para el ambiente local: en AWS llegan de Secrets Manager.
_LOCAL = CredencialesAliado(client_id="local-sintetico", client_secret="local-sintetico")


class Settings(ServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLVENTA_", extra="ignore", hide_input_in_errors=True
    )

    service_name: str = "adaptador-pagos"

    ally_pagos_url: str = Field(
        "http://localhost:8089/pagos", validation_alias=_env("ALLY_PAGOS_URL")
    )
    # 5000 ms en infra (apps/variables.tf, ally_timeouts_ms).
    ally_pagos_timeout_ms: int = Field(5000, validation_alias=_env("ALLY_PAGOS_TIMEOUT_MS"))
    ally_pagos_credentials: CredencialesAliado = Field(
        _LOCAL, validation_alias=_env("ALLY_PAGOS_CREDENTIALS"), repr=False
    )


settings = Settings()
