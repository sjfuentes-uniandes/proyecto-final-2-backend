from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (S1-A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "polizas"

    # HU-W13: insumos de la oferta y reglas de suscripción. La infraestructura inyecta las URL
    # cuando catalog.tf agrega cotizacion y catalogo a polizas.calls (EXT-01).
    cotizacion_url: str = Field("http://localhost:8085", validation_alias=_env("COTIZACION_URL"))
    catalogo_url: str = Field("http://localhost:8084", validation_alias=_env("CATALOGO_URL"))
    # DS2-19 (DEF-03): cotizacion llama a su vez a clientes; siempre bajo el corte de 700 ms.
    cotizacion_timeout_ms: int = Field(600, ge=1, le=700)
    catalogo_timeout_ms: int = Field(300, ge=1, le=700)
    # DS2-18: caché TTL en memoria de las reglas (polizas no tiene CACHE_URL).
    reglas_cache_vigente_s: int = Field(60, ge=0)
    reglas_cache_version_s: int = Field(24 * 3600, ge=0)
    # DS2-14: un caso en revisión más antiguo que esto está fuera del compromiso (E01).
    compromiso_revision_horas: int = Field(8, ge=1)
    # Outbox: la tabla se escribe siempre; la publicación es del relay (EXT-04).
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))


settings = Settings()
