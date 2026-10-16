from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from solventa_common.settings import ServiceSettings
from solventa_common.token_registro import SECRETO_LOCAL


def _env(nombre: str) -> AliasChoices:
    # Acepta el nombre que entrega la infraestructura y su variante SOLVENTA_ (A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLVENTA_", extra="ignore", hide_input_in_errors=True
    )

    service_name: str = "bff-movil"

    clientes_url: str = Field("http://localhost:8083", validation_alias=_env("CLIENTES_URL"))
    # Emisor de cada pool de Cognito: customers, backoffice y partners (infra/apps/ecs.tf).
    jwt_issuers: dict[str, str] = Field(default_factory=dict, validation_alias=_env("JWT_ISSUERS"))
    # Solo la usa POST /v1/registro/validaciones, que existe únicamente con ENVIRONMENT=local
    # para simular el Lambda pre sign-up; en AWS no se necesita ni se entrega.
    registro_token_secret: SecretStr = Field(
        SecretStr(SECRETO_LOCAL), validation_alias=_env("REGISTRO_TOKEN_SECRET"), repr=False
    )

    # Variables propias (SOLVENTA_<NOMBRE>), con valores seguros por defecto.
    # SinProteccion limita el timeout a 700 ms (regla 7).
    clientes_timeout_ms: int = Field(500, ge=1)
    # Debe superar el timeout de `clientes` hacia adaptador-identidad (4 s), que a su vez supera
    # el del proveedor KYC (3 s): cada nivel espera más que el que está debajo.
    verificacion_timeout_ms: int = Field(5000, ge=1)
    # El vínculo es solo una escritura en la base de `clientes`, pero ocurre justo después de
    # crear la cuenta: se tolera un arranque en frío en vez de perder el vínculo.
    vinculacion_timeout_ms: int = Field(3000, ge=1)
    jwks_ttl_s: int = Field(3600, ge=1)
    jwks_timeout_ms: int = Field(500, ge=1)
    jwks_refresco_min_s: int = Field(60, ge=1)


settings = Settings()
