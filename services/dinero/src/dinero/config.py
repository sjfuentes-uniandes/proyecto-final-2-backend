from pydantic import AliasChoices, Field
from solventa_common import outbox
from solventa_common.settings import ServiceSettings

# Timeout de adaptador-pagos hacia el proveedor: ALLY_PAGOS_TIMEOUT_MS (5000 ms en
# infra/apps/variables.tf, ally_timeouts_ms). dinero debe esperar más que eso.
TIMEOUT_ADAPTADOR_PAGOS_MS = 5000


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (S1-A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "dinero"
    # Base (HU-W16, 16.9): la de ServiceSettings, con los nombres que infra/apps/ecs.tf entrega a
    # todo servicio con `database = true`: DB_HOST, DB_PORT, DB_NAME, DB_SSLMODE y DB_POOL_SIZE, y
    # DB_USER y DB_PASSWORD como secretos (o SOLVENTA_DATABASE_URL en local y pruebas). Sin base
    # configurada, la app arranca sin pool.

    # HU-W16 (16.10): cobro por adaptador-pagos. La infraestructura entrega ADAPTADOR_PAGOS_URL
    # (`calls`). El timeout supera al del adaptador (si no, se perdería su respuesta definitiva o su
    # 504) y, con el lote de 2, cabe de sobra en los 60 s de visibilidad de dinero-aceptaciones.
    adaptador_pagos_url: str = Field(
        "http://localhost:8091", validation_alias=_env("ADAPTADOR_PAGOS_URL")
    )
    adaptador_pagos_timeout_ms: int = Field(6500, gt=TIMEOUT_ADAPTADOR_PAGOS_MS, le=10000)
    # Consumidor de OfertaAceptada: activo si llega la cola (`consumes`; en local no llega). Lote y
    # espera larga acotados para que la parada quepa en el stopTimeout de ECS (30 s, ecs.tf:141):
    # 10 s de espera larga más un cobro de hasta 6,5 s.
    sqs_dinero_aceptaciones_url: str | None = Field(
        None, validation_alias=_env("SQS_DINERO_ACEPTACIONES_URL")
    )
    consumidor_max_mensajes: int = Field(2, ge=1, le=2)
    consumidor_espera_larga_s: int = Field(10, ge=1, le=10)
    # Outbox, como polizas (16.6): la infraestructura pone OUTBOX_ENABLED="true" y el tópico a todo
    # servicio que publica (`publishes`); sin la variable (local y pruebas) el relay no arranca.
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))
    # Valores por defecto de solventa_common.outbox.
    outbox_lote: int = Field(outbox.LOTE, ge=1)
    outbox_intervalo_s: float = Field(outbox.INTERVALO_S, gt=0)
    outbox_max_intentos: int = Field(outbox.MAX_INTENTOS, ge=1)
    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))


settings = Settings()
