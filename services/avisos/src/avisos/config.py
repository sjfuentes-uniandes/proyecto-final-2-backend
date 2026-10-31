from pydantic import AliasChoices, Field
from solventa_common import inbox
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_.
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "avisos"

    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack, compose.yaml); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))
    # La infraestructura entrega la cola a avisos (`consumes`, ecs.tf: SQS_AVISOS_URL); sin ella
    # (local y pruebas) el consumidor no arranca. Visibilidad de la cola: 60 s; el primer intento de
    # cada mensaje (un envío de hasta 2 s por dispositivo) debe caber en ella por el tamaño del lote.
    sqs_avisos_url: str | None = Field(None, validation_alias=_env("SQS_AVISOS_URL"))
    consumidor_max_mensajes: int = Field(inbox.MAX_MENSAJES, ge=1, le=10)
    consumidor_espera_larga_s: int = Field(10, ge=1, le=10)

    # Entregas: reintento con espera creciente (no se duerme en el proceso) y barrido de recuperación.
    entregas_max_intentos: int = Field(5, ge=1, le=20)
    entregas_espera_inicial_s: float = Field(5.0, gt=0)
    entregas_espera_max_s: float = Field(300.0, gt=0)
    entregas_barrido_intervalo_s: float = Field(10.0, gt=0)
    # Cuánto reserva una tarea una entrega mientras envía; debe superar el timeout del envío.
    entregas_lease_s: float = Field(60.0, gt=0)

    # HU-M11: envío del push a través de adaptador-firma (infra/platform/catalog.tf: avisos.calls).
    # Sin URL (local y pruebas), el servicio falla cerrado (avisos.adapters.outbound.push_http).
    adaptador_firma_url: str | None = Field(None, validation_alias=_env("ADAPTADOR_FIRMA_URL"))
    # Mayor que el timeout del aliado push en adaptador-firma (2000 ms, ally_timeouts_ms), para que
    # el adaptador siempre pueda responder antes de que avisos agote su propio timeout.
    adaptador_firma_timeout_ms: int = Field(3000, ge=1)


settings = Settings()
