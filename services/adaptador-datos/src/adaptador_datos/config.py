from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Acepta el nombre que entrega la infraestructura y su variante SOLVENTA_.
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class CredencialesAliado(BaseModel):
    """JSON {client_id, client_secret} de Secrets Manager (infra/platform/security.tf)."""

    # hide_input_in_errors: un JSON incompleto no debe mostrar el secreto al fallar el arranque.
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    client_id: SecretStr
    client_secret: SecretStr


# Solo para el ambiente local: en AWS llegan de Secrets Manager (01 §3.2).
_LOCAL = CredencialesAliado(client_id="local-sintetico", client_secret="local-sintetico")


class Settings(ServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLVENTA_", extra="ignore", hide_input_in_errors=True
    )

    service_name: str = "adaptador-datos"

    ally_open_finance_url: str = Field(
        "http://localhost:8089/open-finance", validation_alias=_env("ALLY_OPEN_FINANCE_URL")
    )
    ally_open_finance_timeout_ms: int = Field(
        700, validation_alias=_env("ALLY_OPEN_FINANCE_TIMEOUT_MS")
    )
    ally_open_finance_credentials: CredencialesAliado = Field(
        _LOCAL, validation_alias=_env("ALLY_OPEN_FINANCE_CREDENTIALS"), repr=False
    )
    ally_datos_abiertos_url: str = Field(
        "http://localhost:8089/datos-abiertos", validation_alias=_env("ALLY_DATOS_ABIERTOS_URL")
    )
    ally_datos_abiertos_timeout_ms: int = Field(
        700, validation_alias=_env("ALLY_DATOS_ABIERTOS_TIMEOUT_MS")
    )
    ally_datos_abiertos_credentials: CredencialesAliado = Field(
        _LOCAL, validation_alias=_env("ALLY_DATOS_ABIERTOS_CREDENTIALS"), repr=False
    )
    # Protección (W12, DT-21).
    cb_fallos_consecutivos: int = 5
    cb_apertura_s: int = 30
    bulkhead_max: int = 50


settings = Settings()
