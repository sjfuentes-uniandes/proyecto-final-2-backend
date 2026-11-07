"""Un objeto por lote en el bucket de auditoría (Object Lock, versionado; `infra/platform/storage.tf`).

Clave: `cadena/<secuencia con 12 dígitos>-<hash del lote>.json`. La secuencia ordena la lista; el
hash distingue un objeto huérfano (escrito antes de un commit que falló) del que quedó en la cadena.
Se escribe con `If-None-Match: *`: nunca reemplaza un objeto existente. La retención la pone el
bucket (Object Lock por defecto); `AUDIT_BUCKET_SSE`, si llega, se envía como cifrado del objeto.
"""

import asyncio
import json
from typing import Any

from auditoria.domain.registro import GENESIS, Lote

PREFIJO = "cadena/"
TIPO = "application/json"


def clave(secuencia: int, hash_: str) -> str:
    return f"{PREFIJO}{secuencia:012d}-{hash_}.json"


def clave_de(lote: Lote) -> str:
    return clave(lote.secuencia, lote.hash)


class AlmacenS3:
    def __init__(self, cliente: Any, bucket: str, *, cifrado: str | None = None) -> None:
        self._cliente = cliente
        self._bucket = bucket
        self._cifrado = cifrado

    async def guardar(self, lote: Lote) -> None:
        argumentos: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": clave_de(lote),
            "Body": lote.contenido(),
            "ContentType": TIPO,
            "IfNoneMatch": "*",
        }
        if self._cifrado:
            argumentos["ServerSideEncryption"] = self._cifrado
        await asyncio.to_thread(self._cliente.put_object, **argumentos)

    async def leer_cadena(self, secuencia: int, hash_: str) -> list[dict[str, Any]]:
        """Los documentos de lote desde el primero hasta la cabeza `(secuencia, hash_)`, siguiendo
        `previousHash` hacia atrás: los huérfanos quedan fuera. Para `verificar_cadena`."""
        documentos: list[dict[str, Any]] = []
        while secuencia > 0:
            objeto = await asyncio.to_thread(
                self._cliente.get_object, Bucket=self._bucket, Key=clave(secuencia, hash_)
            )
            documento = json.loads(objeto["Body"].read())
            documentos.append(documento)
            secuencia, hash_ = secuencia - 1, str(documento["previousHash"])
        if hash_ != GENESIS:
            raise ValueError("la cadena no termina en el génesis")
        return documentos[::-1]


def cliente_s3(
    region: str,
    *,
    endpoint_url: str | None = None,
    timeout_conexion_s: float = 1.0,
    timeout_lectura_s: float = 5.0,
    intentos: int = 3,
) -> Any:
    """Cliente S3 con timeouts y reintentos explícitos (`intentos` incluye el primero)."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        config=Config(
            connect_timeout=timeout_conexion_s,
            read_timeout=timeout_lectura_s,
            retries={"total_max_attempts": intentos, "mode": "standard"},
            # LocalStack sirve el bucket por ruta; en AWS es indiferente.
            s3={"addressing_style": "path"} if endpoint_url else None,
        ),
    )
