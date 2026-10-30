from typing import Literal

from pydantic import AliasChoices, Field
from solventa_common import inbox, outbox
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
    # HU-W15, DS2-24: versión vigente del texto de las declaraciones del tomador (F01). Una
    # solicitud con otra versión responde 422 TEXTO_NO_VIGENTE.
    version_declaraciones: str = Field("2026.10.1", min_length=1, max_length=32)
    # Outbox: la tabla se escribe siempre; la publicación es del relay (EXT-04). HU-W16 (16.6,
    # P16-1): la infraestructura pone OUTBOX_ENABLED="true" y el tópico a todo servicio que publica
    # (infra/apps/ecs.tf); sin la variable (local y pruebas) el relay no arranca.
    outbox_enabled: bool = Field(False, validation_alias=_env("OUTBOX_ENABLED"))
    events_topic_arn: str | None = Field(None, validation_alias=_env("EVENTS_TOPIC_ARN"))
    # Valores por defecto de solventa_common.outbox.
    outbox_lote: int = Field(outbox.LOTE, ge=1)
    outbox_intervalo_s: float = Field(outbox.INTERVALO_S, gt=0)
    outbox_max_intentos: int = Field(outbox.MAX_INTENTOS, ge=1)
    aws_region: str = Field("us-east-1", validation_alias=_env("AWS_REGION"))
    # Solo local y pruebas (LocalStack, compose.yaml); en AWS no se define.
    aws_endpoint_url: str | None = Field(None, validation_alias=_env("AWS_ENDPOINT_URL"))
    # HU-W16 (16.13): consumidor de polizas-recaudos (RecaudoConfirmado y RecaudoRechazado). La
    # infraestructura entrega la cola a polizas (`consumes`, infra/apps/ecs.tf:82); sin ella (local
    # y pruebas) no arranca.
    # - Lote: el de la librería (5). Cada mensaje es una transacción corta (inbox y proyección, ms),
    #   muy por debajo de los 120 s de visibilidad de la cola.
    # - Espera larga de 10 s (no 20): la parada se atiende entre ciclos, y con el relay (10 s) debe
    #   caber en el stopTimeout de 30 s (ecs.tf:141). Por eso el tope.
    sqs_polizas_recaudos_url: str | None = Field(
        None, validation_alias=_env("SQS_POLIZAS_RECAUDOS_URL")
    )
    consumidor_max_mensajes: int = Field(inbox.MAX_MENSAJES, ge=1, le=10)
    consumidor_espera_larga_s: int = Field(10, ge=1, le=10)
    # HU-W17 (17.7): documentos de póliza. polizas está en document_writers: la infraestructura le
    # entrega el bucket versionado y su cifrado (infra/apps/ecs.tf:88-91). La política del bucket
    # niega todo PutObject sin este cifrado o con otro (infra/platform/storage.tf). Sin bucket
    # (local y pruebas) no se emiten pólizas, y la app arranca igual.
    polizas_bucket: str | None = Field(None, validation_alias=_env("POLIZAS_BUCKET"))
    polizas_bucket_sse: Literal["AES256", "aws:kms"] = Field(
        "AES256", validation_alias=_env("POLIZAS_BUCKET_SSE")
    )
    # Vencimiento de la URL prefirmada del documento. Se pide al hacer clic (17.9) y el navegador
    # la abre en seguida: 5 minutos cubren la espera, y una URL filtrada (historial, logs) deja de
    # servir pronto. Hasta 1 h, para que siga siendo corta.
    documento_url_vence_s: int = Field(300, ge=60, le=3600)
    # HU-W17: firma de la huella del documento (17.8). adaptador-firma está en polizas.calls
    # (infra/platform/catalog.tf:56; ecs.tf:73).
    adaptador_firma_url: str = Field(
        "http://localhost:8092", validation_alias=_env("ADAPTADOR_FIRMA_URL")
    )


settings = Settings()
