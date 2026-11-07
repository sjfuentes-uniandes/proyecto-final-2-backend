from pydantic import AliasChoices, Field
from solventa_common import outbox
from solventa_common.settings import ServiceSettings


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (S1-A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "pagos-parametricos"

    # HU-S03: un evento paramétrico válido crea el siniestro una sola vez
    # (PagosParametricos --> AtencionSiniestros, modelo_concurrencia.puml (9)).
    siniestros_url: str = Field("http://localhost:8091", validation_alias=_env("SINIESTROS_URL"))
    # Eventos observados y copia local de las coberturas activas (eventos Poliza*).
    sqs_parametricos_eventos_url: str | None = Field(
        None, validation_alias=_env("SQS_PARAMETRICOS_EVENTOS_URL")
    )
    sqs_parametricos_polizas_url: str | None = Field(
        None, validation_alias=_env("SQS_PARAMETRICOS_POLIZAS_URL")
    )
    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack, compose.yaml); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))
    # HU-W24: la DLQ de parametricos-eventos. Sin valor, `<cola>-dlq` (messaging.tf).
    sqs_parametricos_eventos_dlq_url: str | None = Field(
        None, validation_alias=_env("SQS_PARAMETRICOS_EVENTOS_DLQ_URL")
    )
    # maxReceiveCount de parametricos-eventos (catalog.tf): lo muestra H10 como "N de N".
    descarte_max_intentos: int = Field(5, ge=1, validation_alias=_env("DESCARTE_MAX_INTENTOS"))
    # Cuántos mensajes de la DLQ se leen por consulta: la DLQ es pequeña por diseño (AC3).
    descarte_max_mensajes: int = Field(100, ge=1, le=500)
    # HU-W24: Outbox (DescarteParametricoResuelto para auditoria; HU-W21 publica aquí también).
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))
    # Valores por defecto de solventa_common.outbox.
    outbox_lote: int = Field(outbox.LOTE, ge=1)
    outbox_intervalo_s: float = Field(outbox.INTERVALO_S, gt=0)
    outbox_max_intentos: int = Field(outbox.MAX_INTENTOS, ge=1)


settings = Settings()
