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
from urllib.parse import quote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from siniestros.application.siniestro.errores import (
    DependenciaNoDisponible,
    EvidenciaNoCargada,
    EvidenciaNoDisponible,
    IntegridadEvidenciaInvalida,
)
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


def disposicion_descarga(nombre: str) -> str:
    nombre = nombre.replace("\\", "/").rsplit("/", 1)[-1]
    nombre = "".join(c for c in nombre if c.isprintable()).strip() or "evidencia"
    ascii_nombre = nombre.encode("ascii", "ignore").decode() or "evidencia"
    ascii_nombre = ascii_nombre.replace("\\", "\\\\").replace('"', '\\"')
    return f"attachment; filename=\"{ascii_nombre}\"; filename*=UTF-8''{quote(nombre, safe='')}"


class EvidenciasS3:
    def __init__(
        self,
        cliente: Any,
        bucket: str,
        timeout_ms: int,
        cliente_firma: Any | None = None,
        *,
        cliente_conservacion: Any | None = None,
        conservacion_timeout_ms: int = 2000,
    ) -> None:
        self._cliente = cliente
        self._firma = cliente_firma or cliente
        self._bucket = bucket
        self._timeout_s = timeout_ms / 1000
        self._conservacion = cliente_conservacion or cliente
        self._conservacion_timeout_s = conservacion_timeout_ms / 1000

    def _head(self, clave: str, *, cliente: Any | None = None) -> dict[str, Any]:
        respuesta: dict[str, Any] = (cliente or self._cliente).head_object(
            Bucket=self._bucket, Key=clave, ChecksumMode="ENABLED"
        )
        return respuesta

    @staticmethod
    def _validar_archivo(
        datos: dict[str, Any], sha256: str, tamano_bytes: int, tipo_contenido: str
    ) -> None:
        if not datos.get("ChecksumSHA256"):
            raise DependenciaNoDisponible("evidencias")
        if (
            datos["ChecksumSHA256"] != checksum_base64(sha256)
            or datos.get("ContentLength") != tamano_bytes
            or datos.get("ContentType") != tipo_contenido
        ):
            raise IntegridadEvidenciaInvalida

    def _conservar(
        self,
        origen: str,
        destino: str,
        sha256: str,
        tamano_bytes: int,
        tipo_contenido: str,
        cifrado: str,
    ) -> None:
        try:
            existente = self._head(destino, cliente=self._conservacion)
        except ClientError as error:
            codigo = str(error.response.get("Error", {}).get("Code", ""))
            if codigo in {"403", "AccessDenied"}:
                # Sin ListBucket, HEAD de un destino ausente devuelve 403. No se asume ausente:
                # se exige origen verificable, copia autorizada y verificacion final del destino.
                logger.info(
                    "s3: destino no comprobable; se verificara la copia",
                    extra={"error_type": codigo},
                )
            elif codigo not in _AUSENTE:
                raise
        else:
            self._validar_archivo(existente, sha256, tamano_bytes, tipo_contenido)
            return
        fuente = self._head(origen, cliente=self._conservacion)
        self._validar_archivo(fuente, sha256, tamano_bytes, tipo_contenido)
        copia = {"Bucket": self._bucket, "Key": origen}
        if fuente.get("VersionId"):
            copia["VersionId"] = fuente["VersionId"]
        # Se copia exactamente la version verificada, no un objeto sustituido durante el envio.
        self._conservacion.copy_object(
            Bucket=self._bucket,
            Key=destino,
            CopySource=copia,
            CopySourceIfMatch=fuente["ETag"],
            ChecksumAlgorithm="SHA256",
            ServerSideEncryption=cifrado,
            MetadataDirective="COPY",
        )
        self._validar_archivo(
            self._head(destino, cliente=self._conservacion), sha256, tamano_bytes, tipo_contenido
        )

    async def conservar(
        self,
        origen: str,
        destino: str,
        sha256: str,
        tamano_bytes: int,
        tipo_contenido: str,
        cifrado: str,
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._conservar, origen, destino, sha256, tamano_bytes, tipo_contenido, cifrado
                ),
                timeout=self._conservacion_timeout_s,
            )
        except ClientError as error:
            codigo = str(error.response.get("Error", {}).get("Code", ""))
            if codigo in _AUSENTE:
                raise EvidenciaNoCargada from error
            logger.warning("s3: no se pudo conservar la evidencia", extra={"error_type": codigo})
            raise DependenciaNoDisponible("evidencias") from error
        except (TimeoutError, BotoCoreError) as error:
            logger.warning(
                "s3: no se pudo conservar la evidencia", extra={"error_type": type(error).__name__}
            )
            raise DependenciaNoDisponible("evidencias") from error

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

    def _descarga(
        self, clave: str, sha256: str, ttl_s: int, tipo_contenido: str, nombre_archivo: str
    ) -> str:
        try:
            datos = self._head(clave)
        except ClientError as error:
            codigo = str(error.response.get("Error", {}).get("Code", ""))
            if codigo in _AUSENTE:
                raise EvidenciaNoDisponible from error
            if codigo in {"403", "AccessDenied"} and clave.startswith("siniestros/"):
                listado = self._cliente.list_objects_v2(
                    Bucket=self._bucket, Prefix=clave, MaxKeys=1
                )
                if not any(o["Key"] == clave for o in listado.get("Contents", [])):
                    raise EvidenciaNoDisponible from error
            raise
        if not datos.get("ChecksumSHA256"):
            raise DependenciaNoDisponible("evidencias")
        if datos["ChecksumSHA256"] != checksum_base64(sha256):
            raise IntegridadEvidenciaInvalida
        parametros = {
            "Bucket": self._bucket,
            "Key": clave,
            "ResponseContentType": tipo_contenido,
            "ResponseContentDisposition": disposicion_descarga(nombre_archivo),
        }
        if datos.get("VersionId"):
            parametros["VersionId"] = datos["VersionId"]
        url: str = self._firma.generate_presigned_url(
            "get_object", Params=parametros, ExpiresIn=ttl_s
        )
        destino = urlsplit(url)
        if destino.scheme != "https" or not destino.netloc:
            logger.error("s3: la descarga requiere un endpoint HTTPS")
            raise DependenciaNoDisponible("evidencias")
        return url

    async def firmar_descarga(
        self, clave: str, sha256: str, ttl_s: int, tipo_contenido: str, nombre_archivo: str
    ) -> str:
        if not 1 <= ttl_s <= 300:
            raise ValueError("La vigencia de descarga debe estar entre 1 y 300 segundos")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._descarga, clave, sha256, ttl_s, tipo_contenido, nombre_archivo
                ),
                timeout=self._timeout_s,
            )
        except (ClientError, BotoCoreError, TimeoutError) as error:
            logger.warning(
                "s3: no se pudo emitir descarga", extra={"error_type": type(error).__name__}
            )
            raise DependenciaNoDisponible("evidencias") from error

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

    def _post(
        self,
        clave: str,
        tipo_contenido: str,
        tamano_bytes: int,
        sha256: str,
        ttl_s: int,
        cifrado: str,
    ) -> dict[str, Any]:
        checksum = checksum_base64(sha256)
        campos = {
            "key": clave,
            "Content-Type": tipo_contenido,
            "x-amz-checksum-algorithm": "SHA256",
            "x-amz-checksum-sha256": checksum,
            "x-amz-server-side-encryption": cifrado,
        }
        condiciones: list[Any] = [
            {"key": clave},
            {"Content-Type": tipo_contenido},
            {"x-amz-checksum-algorithm": "SHA256"},
            {"x-amz-checksum-sha256": checksum},
            {"x-amz-server-side-encryption": cifrado},
            ["content-length-range", tamano_bytes, tamano_bytes],
        ]
        return self._firma.generate_presigned_post(
            Bucket=self._bucket,
            Key=clave,
            Fields=campos,
            Conditions=condiciones,
            ExpiresIn=ttl_s,
        )

    async def firmar_carga(
        self,
        clave: str,
        tipo_contenido: str,
        tamano_bytes: int,
        sha256: str,
        ttl_s: int,
        cifrado: str,
    ) -> tuple[str, dict[str, str]]:
        try:
            respuesta = await asyncio.to_thread(
                self._post, clave, tipo_contenido, tamano_bytes, sha256, ttl_s, cifrado
            )
        except (ClientError, BotoCoreError) as error:
            logger.error(
                "s3: no se pudo firmar la carga", extra={"error_type": type(error).__name__}
            )
            raise DependenciaNoDisponible("evidencias") from error
        url = respuesta.get("url")
        campos = respuesta.get("fields")
        if not isinstance(url, str) or not isinstance(campos, dict):
            raise DependenciaNoDisponible("evidencias")
        return url, {str(clave): str(valor) for clave, valor in campos.items()}


class AlmacenSinConfigurar:
    """Sin `EVIDENCIAS_BUCKET` (local sin LocalStack): no se puede verificar ni firmar."""

    async def verificar(self, clave: str, sha256: str) -> IntegridadEvidencia:
        return IntegridadEvidencia.NO_VERIFICABLE

    async def conservar(
        self,
        origen: str,
        destino: str,
        sha256: str,
        tamano_bytes: int,
        tipo_contenido: str,
        cifrado: str,
    ) -> None:
        raise DependenciaNoDisponible("evidencias")

    async def firmar_lectura(self, clave: str, ttl_s: int, tipo_contenido: str) -> str:
        raise DependenciaNoDisponible("evidencias")

    async def firmar_descarga(
        self, clave: str, sha256: str, ttl_s: int, tipo_contenido: str, nombre_archivo: str
    ) -> str:
        raise DependenciaNoDisponible("evidencias")

    async def firmar_carga(
        self,
        clave: str,
        tipo_contenido: str,
        tamano_bytes: int,
        sha256: str,
        ttl_s: int,
        cifrado: str,
    ) -> tuple[str, dict[str, str]]:
        raise DependenciaNoDisponible("evidencias")
