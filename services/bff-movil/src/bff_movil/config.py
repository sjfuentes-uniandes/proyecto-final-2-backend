from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Acepta el nombre que entrega la infraestructura y su variante SOLVENTA_ (A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "bff-movil"

    clientes_url: str = Field("http://localhost:8083", validation_alias=_env("CLIENTES_URL"))
    # Emisor de cada pool de Cognito: customers, backoffice y partners (infra/apps/ecs.tf).
    jwt_issuers: dict[str, str] = Field(default_factory=dict, validation_alias=_env("JWT_ISSUERS"))

    # Variables propias (SOLVENTA_<NOMBRE>), con valores seguros por defecto.
    # SinProteccion limita el timeout a 700 ms (regla 7).
    clientes_timeout_ms: int = Field(500, ge=1)
    jwks_ttl_s: int = Field(3600, ge=1)
    jwks_timeout_ms: int = Field(500, ge=1)
    jwks_refresco_min_s: int = Field(60, ge=1)


settings = Settings()
