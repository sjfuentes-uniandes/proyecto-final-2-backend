from typing import Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import SettingsConfigDict
from solventa_common import presupuesto
from solventa_common.settings import ServiceSettings

# HU-W05: presupuesto del recorrido de cotización si LATENCY_BUDGET_MS no trae "cotizacion".
# El de cada ambiente vive en latency_budget_ms de infra/envs/<env>/apps.tfvars.
PRESUPUESTO_RECORRIDO_MS = 700
OPERACION_RECORRIDO = "cotizacion"


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (01 §3.2, A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    # Un error de validación al arrancar no debe imprimir la configuración (DB_PASSWORD).
    model_config = SettingsConfigDict(
        env_prefix="SOLVENTA_", extra="ignore", hide_input_in_errors=True
    )

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
    # HU-W03: meta p95 de la operación cotizacion (latency_targets en int) que se informa al
    # portal, y caché del producto y las reglas. Con CACHE_URL se lee Valkey (lo llena catalogo);
    # sin ella, caché en memoria de catalogo_cache_ttl_s (0 = sin caché).
    cotizacion_presupuesto_ms: int = Field(250, ge=1)
    # HU-W13 (DS2-20): vigencia de la oferta en horas desde el cálculo; vigenteHasta se deriva al
    # leer. Configurable por ambiente con SOLVENTA_OFERTA_VIGENCIA_HORAS (0 = nace vencida).
    oferta_vigencia_horas: int = Field(96, ge=0)
    cache_url: str | None = Field(None, validation_alias=_env("CACHE_URL"), repr=False)
    cache_timeout_ms: int = Field(50, ge=1)
    catalogo_cache_ttl_s: int = Field(60, ge=0)
    # Outbox: la tabla se escribe siempre; la publicación es del relay (DT-15).
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))
    # HU-W05: presupuesto por operación (JSON de latency_budget_ms). Solo se usa "cotizacion": el
    # deadline del recorrido, que viaja en X-Deadline-Ms a catalogo y clientes.
    latency_budget_ms: dict[str, int] = Field(
        default_factory=dict, validation_alias=_env("LATENCY_BUDGET_MS")
    )
    # Protección de catalogo y clientes en el recorrido (resiliencia.ProteccionDependencia):
    # circuito por dependencia y bulkhead por servicio, como adaptador-datos (DT-21).
    cb_fallos_consecutivos: int = Field(5, ge=1)
    cb_apertura_s: int = Field(30, ge=1)
    bulkhead_max: int = Field(50, ge=1)
    # Pool de httpx (docs/hu-w05-limite-tiempo.md §Conexiones): con cupo para los bulkheads de
    # catalogo y clientes más las ramas del perfilamiento, el que rechaza es el bulkhead (sin
    # esperar) y no el pool (que haría esperar y consumiría el presupuesto).
    http_max_conexiones: int = Field(150, ge=1)
    http_max_keepalive: int = Field(40, ge=0)

    @property
    def presupuesto_recorrido_ms(self) -> int:
        return self.latency_budget_ms.get(OPERACION_RECORRIDO, PRESUPUESTO_RECORRIDO_MS)

    @model_validator(mode="after")
    def _timeouts_dentro_del_presupuesto(self) -> Self:
        # Si un timeout de dependencia no es menor que el presupuesto, el servicio no arranca.
        presupuesto.por_operacion(self.latency_budget_ms)
        presupuesto.validar_timeouts(
            OPERACION_RECORRIDO,
            self.presupuesto_recorrido_ms,
            {"dependencia_timeout_ms": self.dependencia_timeout_ms},
        )
        if self.http_max_conexiones < 2 * self.bulkhead_max:
            raise ValueError("http_max_conexiones debe cubrir los bulkheads de catalogo y clientes")
        return self


settings = Settings()
