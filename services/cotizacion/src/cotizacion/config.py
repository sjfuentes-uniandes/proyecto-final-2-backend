from pydantic import AliasChoices, Field
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (01 §3.2, A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "cotizacion"

    # SOLVENTA_CLIENTES_URL tiene prioridad sobre CLIENTES_URL (se conserva por compatibilidad).
    clientes_url: str = Field(
        "http://localhost:8083",
        validation_alias=AliasChoices("SOLVENTA_CLIENTES_URL", "CLIENTES_URL"),
    )
    catalogo_url: str = Field("http://localhost:8084", validation_alias=_env("CATALOGO_URL"))
    adaptador_datos_url: str = Field(
        "http://localhost:8086", validation_alias=_env("ADAPTADOR_DATOS_URL")
    )
    # Presupuestos de DT-10. La rama de cada fuente nunca pasa del corte duro de 700 ms.
    perfilamiento_presupuesto_ms: int = Field(400, ge=1)
    dependencia_timeout_ms: int = Field(150, ge=1)
    fuente_timeout_ms: int = Field(300, ge=20, le=700)
    modelo_cache_ttl_s: int = Field(60, ge=0)
    # Outbox: la tabla se escribe siempre; la publicación es del relay (DT-15).
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))


settings = Settings()
