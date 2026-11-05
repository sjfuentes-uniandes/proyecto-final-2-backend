"""AlmacenEvidencias sobre S3 (HU-W19, DS3-06 y DS3-08). boto3 es síncrono: cada llamada corre en un
hilo (`asyncio.to_thread`) con su corte, así no bloquea el bucle ni retiene una conexión de base.

- `verificar`: `HeadObject` con `ChecksumMode=ENABLED`; compara el `ChecksumSHA256` del objeto con el
  `sha256` registrado, **sin descargar**. Ausente → `NO_DISPONIBLE`; distinto → `ALTERADA`; sin
  checksum, con error o pasado el corte → `NO_VERIFICABLE`.
- `firmar_lectura`: URL prefirmada de `GetObject`. La firma es local (no llama a S3). En local el
  *endpoint* con el que se firma es el público (`localhost`), no el de la red de compose.

Nunca se registran la clave del objeto, la URL ni su firma: los fallos se registran solo por tipo.
"""

import asyncio
import base64
import logging
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from siniestros.application.siniestro.errores import DependenciaNoDisponible
from siniestros.domain.siniestro.modelos import IntegridadEvidencia

logger = logging.getLogger("siniestros.evidencias")

_AUSENTE = frozenset({"404", "NoSuchKey", "NotFound"})
# `total_max_attempts` cuenta el primer intento: 1 = sin reintentos (`max_attempts` cuenta solo los
# reintentos y dejaría dos intentos).
_REINTENTOS = {"total_max_attempts": 1, "mode": "standard"}


def _configuracion(timeout_s: float, *, ruta: bool) -> Config:
    return Config(
        signature_version="s3v4",
        connect_timeout=timeout_s,
        read_timeout=timeout_s,
        retries=_REINTENTOS,
        s3={"addressing_style": "path"} if ruta else {},
    )


def cliente_s3(region: str, timeout_ms: int, endpoint_url: str | None = None) -> Any:
    """Cliente S3 con corte y sin reintentos. Con `endpoint_url` (LocalStack) usa rutas, no
    subdominios del bucket."""
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=_configuracion(timeout_ms / 1000, ruta=endpoint_url is not None),
    )


def checksum_base64(sha256_hex: str) -> str:
    """S3 entrega el SHA-256 en base64 del digest binario."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


class EvidenciasS3:
    def __init__(
        self,
        cliente: Any,
        bucket: str,
        timeout_ms: int,
        cliente_firma: Any | None = None,
    ) -> None:
        self._cliente = cliente
        self._firma = cliente_firma or cliente
        self._bucket = bucket
        self._timeout_s = timeout_ms / 1000

    def _head(self, clave: str) -> dict[str, Any]:
        respuesta: dict[str, Any] = self._cliente.head_object(
            Bucket=self._bucket, Key=clave, ChecksumMode="ENABLED"
        )
        return respuesta

    async def verificar(self, clave: str, sha256: str) -> IntegridadEvidencia:
        try:
            # El corte duro lo da asyncio; el de boto3 evita dejar el hilo colgado.
            respuesta = await asyncio.wait_for(
                asyncio.to_thread(self._head, clave), timeout=self._timeout_s
            )
        except ClientError as error:
            codigo = str(error.response.get("Error", {}).get("Code", ""))
            if codigo in _AUSENTE:
                return IntegridadEvidencia.NO_DISPONIBLE
            logger.warning(
                "s3: no se pudo verificar", extra={"error_type": codigo or "ClientError"}
            )
            return IntegridadEvidencia.NO_VERIFICABLE
        except (TimeoutError, BotoCoreError) as error:
            logger.warning("s3: no se pudo verificar", extra={"error_type": type(error).__name__})
            return IntegridadEvidencia.NO_VERIFICABLE

        checksum = respuesta.get("ChecksumSHA256")
        if not checksum:
            return IntegridadEvidencia.NO_VERIFICABLE
        if checksum == checksum_base64(sha256):
            return IntegridadEvidencia.VERIFICADA
        return IntegridadEvidencia.ALTERADA

    async def firmar_lectura(self, clave: str, ttl_s: int, tipo_contenido: str) -> str:
        try:
            url: str = await asyncio.to_thread(
                self._firma.generate_presigned_url,
                "get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": clave,
                    "ResponseContentType": tipo_contenido,
                },
                ExpiresIn=ttl_s,
            )
        except (ClientError, BotoCoreError) as error:
            logger.error("s3: no se pudo firmar", extra={"error_type": type(error).__name__})
            raise DependenciaNoDisponible("evidencias") from error
        return url


class AlmacenSinConfigurar:
    """Sin `EVIDENCIAS_BUCKET` (local sin LocalStack): no se puede verificar ni firmar."""

    async def verificar(self, clave: str, sha256: str) -> IntegridadEvidencia:
        return IntegridadEvidencia.NO_VERIFICABLE

    async def firmar_lectura(self, clave: str, ttl_s: int, tipo_contenido: str) -> str:
        raise DependenciaNoDisponible("evidencias")
