from uuid import UUID

from pydantic import AliasChoices, Field, model_validator
from solventa_common import outbox
from solventa_common.settings import ServiceSettings

from siniestros.domain.siniestro.coherencia import DISTANCIA_MAX_M
from siniestros.domain.siniestro.plazos import (
    COMPROMISO_HORAS,
    UMBRAL_ALTA_MIN,
    UMBRAL_MEDIA_MIN,
)

# DS3-08: una URL prefirmada de evidencia vive 5 minutos como máximo.
URL_EVIDENCIA_TTL_MAX_S = 300


def _env(nombre: str) -> AliasChoices:
    # Nombre que entrega la infraestructura y su variante SOLVENTA_ (S1-A6).
    return AliasChoices(nombre, f"SOLVENTA_{nombre}")


class Settings(ServiceSettings):
    service_name: str = "siniestros"

    # HU-S03: siniestros es el único servicio con acceso al bucket de evidencias
    # (evidence_writers y evidence_readers, infra/apps/iam.tf). La política del bucket
    # rechaza un PutObject sin el encabezado de cifrado EVIDENCIAS_BUCKET_SSE.
    evidencias_bucket: str | None = Field(None, validation_alias=_env("EVIDENCIAS_BUCKET"))
    evidencias_bucket_sse: str = Field("AES256", validation_alias=_env("EVIDENCIAS_BUCKET_SSE"))
    # Resultado de la indemnización (modelo_concurrencia.puml (8)); sin la cola no arranca el consumidor.
    sqs_siniestros_indemnizaciones_url: str | None = Field(
        None, validation_alias=_env("SQS_SINIESTROS_INDEMNIZACIONES_URL")
    )

    # Solo local (LocalStack): el *endpoint* con el que se firman las URL, alcanzable desde el
    # navegador del analista (http://localhost:4566); el de la red de compose no lo es. En AWS no se
    # define y se firma con el regional.
    evidencias_endpoint_publico: str | None = Field(
        None, validation_alias=_env("EVIDENCIAS_ENDPOINT_PUBLICO")
    )

    # HU-W19 (DS3-08): URL prefirmada de lectura. Nunca más de 300 s; no se guarda ni se registra.
    evidencia_url_ttl_s: int = Field(
        URL_EVIDENCIA_TTL_MAX_S,
        ge=1,
        le=URL_EVIDENCIA_TTL_MAX_S,
        validation_alias=_env("EVIDENCIA_URL_TTL_S"),
    )
    # Corte de cada HeadObject al verificar la integridad (DS3-06). Si vence, la evidencia queda
    # NO_VERIFICABLE y el detalle responde igual.
    s3_timeout_ms: int = Field(300, ge=1, le=5000, validation_alias=_env("S3_TIMEOUT_MS"))

    # HU-W19 (DS3-04): compromiso de 48 h y umbrales de la prioridad derivada (ALTA < 8 h,
    # MEDIA < 24 h de tiempo restante).
    compromiso_horas: int = Field(COMPROMISO_HORAS, ge=1, validation_alias=_env("COMPROMISO_HORAS"))
    prioridad_alta_min: int = Field(
        UMBRAL_ALTA_MIN, ge=1, validation_alias=_env("PRIORIDAD_ALTA_MIN")
    )
    prioridad_media_min: int = Field(
        UMBRAL_MEDIA_MIN, ge=1, validation_alias=_env("PRIORIDAD_MEDIA_MIN")
    )
    # HU-W19 (DS3-06): distancia máxima entre la ubicación declarada y la de la primera captura.
    coherencia_distancia_max_m: int = Field(
        DISTANCIA_MAX_M, ge=1, validation_alias=_env("COHERENCIA_DISTANCIA_MAX_M")
    )

    # HU-W19 (DS3-07): semilla sintética de QA (R7). Apagada por omisión; solo local e INT la
    # encienden, nunca producción.
    semilla_qa: bool = Field(False, validation_alias=_env("SEMILLA_SINIESTROS_QA"))
    # En INT, el `sub` real del usuario de prueba que será el analista de los casos sembrados; sin él
    # se usa el del archivo de la semilla.
    semilla_analista_id: UUID | None = Field(
        None, validation_alias=_env("SEMILLA_SINIESTROS_ANALISTA_ID")
    )

    # Outbox (como dinero y polizas): la infraestructura pone OUTBOX_ENABLED="true" y el tópico a
    # todo servicio que publica (`publishes`); sin la variable el relay no arranca.
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))
    outbox_lote: int = Field(outbox.LOTE, ge=1)
    outbox_intervalo_s: float = Field(outbox.INTERVALO_S, gt=0)
    outbox_max_intentos: int = Field(outbox.MAX_INTENTOS, ge=1)
    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))

    @model_validator(mode="after")
    def _umbrales_ordenados(self) -> "Settings":
        if self.prioridad_alta_min >= self.prioridad_media_min:
            raise ValueError("prioridad_alta_min debe ser menor que prioridad_media_min")
        return self


settings = Settings()
