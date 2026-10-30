"""Almacén de los documentos de póliza en S3 (HU-W17, 17.7; ADR 0014, decisión 4). Implementa
AlmacenDocumentos. boto3 es síncrono: cada llamada corre en un hilo (`asyncio.to_thread`), como el
relay y el consumidor de `solventa_common`.

- `guardar`: `PutObject` con `ServerSideEncryption` = `POLIZAS_BUCKET_SSE` y `ContentType` PDF. La
  política del bucket (`infra/platform/storage.tf`, `DenyUnencryptedWrites` y `DenyOtherEncryption`)
  niega todo `PutObject` sin `x-amz-server-side-encryption` o con otro valor que el del bucket
  (`AES256`, o `aws:kms` con una clave propia). No pone condiciones sobre el id de la clave KMS, y
  la infraestructura no lo entrega: no se envía `SSEKMSKeyId`.
- El bucket está versionado: cada `PutObject` devuelve un `VersionId`, que es la referencia estable
  del documento. Sin él (bucket no versionado) es un fallo.
- `url_descarga`: URL prefirmada (SigV4) de `GetObject` de esa versión, como adjunto con el nombre
  del último segmento de la clave (`<numero>.pdf`). La firma se calcula en local, sin red, pero puede
  necesitar resolver las credenciales de la tarea: también va en un hilo. Apunta al endpoint del
  cliente: en local, al de LocalStack (plan §b, decisión 12).
- Los errores de S3 son `FalloAlmacenDocumentos`, con un código para la emisión (17.8) y sin el
  mensaje de S3, que puede traer el bucket y la clave.
"""

import asyncio
import logging
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from polizas.config import Settings
from polizas.domain.poliza.errores import FalloAlmacenDocumentos

CONTENIDO_PDF: Final = "application/pdf"
# Un PDF de pocos KB: la subida cabe de sobra en 5 s. Dos intentos en total, como SNS en el relay.
TIMEOUT_CONEXION_S: Final = 1.0
TIMEOUT_LECTURA_S: Final = 5.0
INTENTOS_S3: Final = 2
MENSAJE_SIN_BUCKET: Final = "documentos: sin bucket; no se emiten pólizas"
MENSAJE_SIN_CLIENTE: Final = "documentos: no se pudo crear el cliente S3; no se emiten pólizas"

logger = logging.getLogger("polizas.documentos")


def cliente_s3(region: str, *, endpoint_url: str | None = None) -> Any:
    """Cliente S3 de boto3 con SigV4, timeouts y reintentos explícitos (`INTENTOS_S3` incluye el
    primero). Con un endpoint propio (LocalStack), botocore ya usa rutas
    (`<endpoint>/<bucket>/<clave>`)."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=Config(
            signature_version="s3v4",
            connect_timeout=TIMEOUT_CONEXION_S,
            read_timeout=TIMEOUT_LECTURA_S,
            retries={"total_max_attempts": INTENTOS_S3, "mode": "standard"},
        ),
    )


def _fallo(error: Exception) -> FalloAlmacenDocumentos:
    if isinstance(error, ClientError):
        respuesta: Any = error.response
        codigo_s3 = respuesta.get("Error", {}).get("Code")
        estado = respuesta.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
        codigo = (
            FalloAlmacenDocumentos.RECHAZADO
            if 400 <= estado < 500
            else FalloAlmacenDocumentos.NO_DISPONIBLE
        )
        return FalloAlmacenDocumentos(codigo, codigo_s3)
    return FalloAlmacenDocumentos(FalloAlmacenDocumentos.NO_DISPONIBLE)


class AlmacenDocumentosS3:
    """Implementa AlmacenDocumentos sobre un bucket versionado."""

    def __init__(self, cliente: Any, bucket: str, cifrado: str) -> None:
        self.cliente = cliente
        self.bucket = bucket
        self.cifrado = cifrado

    async def guardar(self, clave: str, contenido: bytes) -> str:
        try:
            respuesta = await asyncio.to_thread(
                self.cliente.put_object,
                Bucket=self.bucket,
                Key=clave,
                Body=contenido,
                ContentType=CONTENIDO_PDF,
                ServerSideEncryption=self.cifrado,
            )
        except (BotoCoreError, ClientError) as error:
            raise _fallo(error) from error
        version = respuesta.get("VersionId")
        # Un bucket con el versionado suspendido responde la versión "null".
        if not version or version == "null":
            raise FalloAlmacenDocumentos(FalloAlmacenDocumentos.SIN_VERSION)
        return str(version)

    async def url_descarga(self, clave: str, version_id: str, vence_s: int) -> str:
        if vence_s < 1:
            raise ValueError("vence_s: se espera un entero positivo")
        nombre = clave.rsplit("/", 1)[-1]
        try:
            url = await asyncio.to_thread(
                self.cliente.generate_presigned_url,
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": clave,
                    "VersionId": version_id,
                    "ResponseContentDisposition": f'attachment; filename="{nombre}"',
                },
                ExpiresIn=vence_s,
            )
        except (BotoCoreError, ClientError) as error:
            raise _fallo(error) from error
        return str(url)


def almacen_desde(settings: Settings) -> AlmacenDocumentosS3 | None:
    """El almacén de la configuración; None (y se registra) sin bucket o sin cliente. La app arranca
    igual: sin almacén no se emiten pólizas, y las EMITIENDO esperan (17.8)."""
    bucket = settings.polizas_bucket
    if not bucket:
        logger.info(MENSAJE_SIN_BUCKET, extra={"missing": "POLIZAS_BUCKET"})
        return None
    try:
        cliente = cliente_s3(settings.aws_region, endpoint_url=settings.aws_endpoint_url)
    except Exception as error:  # noqa: BLE001 - el mensaje puede traer la configuración
        logger.error(MENSAJE_SIN_CLIENTE, extra={"error_type": type(error).__name__})
        return None
    return AlmacenDocumentosS3(cliente, bucket, settings.polizas_bucket_sse)
