"""El original de cada evento en el bucket de evidencias (HU-W21, DS3-16; HU-S03).

boto3 es síncrono: cada `PutObject` corre en un hilo (`asyncio.to_thread`) con su corte
(`s3_timeout_ms`, 300 ms por omisión) y **sin retener ninguna conexión de base**. boto3 no reintenta
(`total_max_attempts: 1`, R-28): el reintento lo decide este adaptador, **uno solo** y solo si el
primer intento falló en el transporte (corte, conexión caída o cerrada), dentro del corte total de
700 ms (§13, regla 7). Después de unos minutos sin envíos, la conexión que el cliente reutiliza
puede no responder y el primer `PutObject` se pasa del corte; el reintento abre otra (R-225). Un
error de S3 (`ClientError`: permisos, cifrado) no se reintenta. Si tampoco alcanza, la fuente
reenvía el mismo evento (503).

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
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as ConexionBotocore

from adaptador_telemetria.application.telemetria.puertos import DependenciaNoDisponible

logger = logging.getLogger("adaptador_telemetria.archivo")

DEPENDENCIA = "archivo"
MENSAJE_FALLO = "s3: no se pudo archivar el original"
MENSAJE_REINTENTO = "s3: el archivo del original no respondió; se reintenta una vez"
# §13, regla 7: ninguna llamada a una dependencia pasa de 700 ms, contando el reintento.
CORTE_TOTAL_MS = 700
# Un reintento con menos tiempo que este no alcanza a abrir otra conexión: no se intenta.
REINTENTO_MINIMO_MS = 100
# Fallas de transporte: el objeto puede no haber llegado y otra conexión puede funcionar.
FALLAS_TRANSPORTE = (TimeoutError, ConexionBotocore, HTTPClientError)
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

    async def _intentar(
        self, clave: str, cuerpo: bytes, metadatos: Mapping[str, str], timeout_s: float
    ) -> None:
        # El corte duro lo da asyncio; el de boto3 evita dejar el hilo colgado.
        await asyncio.wait_for(
            asyncio.to_thread(self._poner, clave, cuerpo, metadatos), timeout=timeout_s
        )

    async def guardar(self, clave: str, cuerpo: bytes, metadatos: Mapping[str, str]) -> None:
        bucle = asyncio.get_running_loop()
        limite = bucle.time() + CORTE_TOTAL_MS / 1000
        try:
            try:
                await self._intentar(clave, cuerpo, metadatos, self._timeout_s)
            except FALLAS_TRANSPORTE as error:
                restante_s = limite - bucle.time()
                if restante_s < REINTENTO_MINIMO_MS / 1000:
                    raise
                # La clave es determinista: si el primer PutObject sí llegó, el segundo escribe
                # el mismo objeto.
                logger.warning(MENSAJE_REINTENTO, extra={"error_type": type(error).__name__})
                await self._intentar(clave, cuerpo, metadatos, min(self._timeout_s, restante_s))
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
