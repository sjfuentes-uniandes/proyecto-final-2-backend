"""El original de cada evento en el bucket de evidencias (HU-W21, DS3-16; HU-S03).

boto3 es síncrono: cada `PutObject` corre en un hilo (`asyncio.to_thread`) con su corte
(`s3_timeout_ms`, ≤ 700 ms) y **sin retener ninguna conexión de base**. Sin reintentos
(`total_max_attempts: 1`, R-28): si falla o se pasa del corte, la fuente reenvía el mismo evento.

Cada objeto lleva el cifrado que exige la política del bucket (`ServerSideEncryption`) y su
`ChecksumSHA256`. La clave es determinista, así que repetir el envío escribe el mismo objeto. Solo
se registra el tipo del error, nunca la clave ni el cuerpo.
"""

import asyncio
import base64
import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from adaptador_telemetria.application.telemetria.puertos import DependenciaNoDisponible

logger = logging.getLogger("adaptador_telemetria.archivo")

DEPENDENCIA = "archivo"
MENSAJE_FALLO = "s3: no se pudo archivar el original"
MENSAJE_SIN_BUCKET = "archivo: sin EVIDENCIAS_BUCKET; no se acepta ningún evento"


def cliente_s3(region: str, timeout_ms: int, endpoint_url: str | None = None) -> Any:
    """Cliente S3 con corte y sin reintentos. Con `endpoint_url` (LocalStack) usa rutas, no
    subdominios del bucket."""
    segundos = timeout_ms / 1000
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=Config(
            signature_version="s3v4",
            connect_timeout=segundos,
            read_timeout=segundos,
            retries={"total_max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"} if endpoint_url is not None else {},
        ),
    )


class ArchivoS3:
    def __init__(self, cliente: Any, bucket: str, sse: str, timeout_ms: int) -> None:
        self._cliente = cliente
        self._bucket = bucket
        self._sse = sse
        self._timeout_s = timeout_ms / 1000

    def _poner(self, clave: str, cuerpo: bytes, metadatos: Mapping[str, str]) -> None:
        self._cliente.put_object(
            Bucket=self._bucket,
            Key=clave,
            Body=cuerpo,
            ContentType="application/json",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(cuerpo).digest()).decode(),
            # La política del bucket rechaza cualquier otro cifrado (HU-S03).
            ServerSideEncryption=self._sse,
            Metadata=dict(metadatos),
        )

    async def guardar(self, clave: str, cuerpo: bytes, metadatos: Mapping[str, str]) -> None:
        try:
            # El corte duro lo da asyncio; el de boto3 evita dejar el hilo colgado.
            await asyncio.wait_for(
                asyncio.to_thread(self._poner, clave, cuerpo, metadatos), timeout=self._timeout_s
            )
        except ClientError as error:
            codigo = str(error.response.get("Error", {}).get("Code", "")) or "ClientError"
            logger.error(MENSAJE_FALLO, extra={"error_type": codigo})
            raise DependenciaNoDisponible(DEPENDENCIA) from None
        except (TimeoutError, BotoCoreError) as error:
            logger.error(MENSAJE_FALLO, extra={"error_type": type(error).__name__})
            raise DependenciaNoDisponible(DEPENDENCIA) from None


class ArchivoSinConfigurar:
    """Sin `EVIDENCIAS_BUCKET` (local sin LocalStack): un evento sin su original no se acepta."""

    async def guardar(self, clave: str, cuerpo: bytes, metadatos: Mapping[str, str]) -> None:
        raise DependenciaNoDisponible(DEPENDENCIA)
