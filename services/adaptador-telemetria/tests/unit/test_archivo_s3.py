"""El original en S3 (HU-W21, DS3-16; HU-S03): cifrado exigido, checksum, corte y sin reintentos."""

import base64
import hashlib
import time
from typing import Any

import pytest
from adaptador_telemetria.adapters.outbound.archivo_s3 import (
    ArchivoS3,
    ArchivoSinConfigurar,
    cliente_s3,
)
from adaptador_telemetria.application.telemetria.puertos import DependenciaNoDisponible
from botocore.exceptions import ClientError, EndpointConnectionError

pytestmark = pytest.mark.anyio
CLAVE = "telemetria/2026/11/10/bafc2b26-7c88-5ae3-b5ac-26fdf5427a09.json"
CUERPO = b'{"valorObservado":"valor-sensible-135"}'
METADATOS = {"fuente": "IDEAM-SIM", "id-externo": "IDEAM-1", "evento-id": "e", "recibido-en": "t"}


class ClienteFalso:
    def __init__(self, error: Exception | None = None, demora_s: float = 0) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.error = error
        self.demora_s = demora_s

    def put_object(self, **argumentos: Any) -> dict[str, Any]:
        if self.demora_s:
            time.sleep(self.demora_s)
        if self.error:
            raise self.error
        self.llamadas.append(argumentos)
        return {}


async def test_el_objeto_lleva_cifrado_checksum_tipo_y_metadatos() -> None:
    cliente = ClienteFalso()
    await ArchivoS3(cliente, "bucket-evidencias", "aws:kms", 300).guardar(CLAVE, CUERPO, METADATOS)
    [llamada] = cliente.llamadas
    assert llamada["Bucket"] == "bucket-evidencias"
    assert llamada["Key"] == CLAVE
    assert llamada["Body"] == CUERPO
    # La política del bucket rechaza cualquier otro cifrado.
    assert llamada["ServerSideEncryption"] == "aws:kms"
    assert llamada["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(CUERPO).digest()).decode()
    assert llamada["ContentType"] == "application/json"
    assert llamada["Metadata"] == METADATOS


async def test_repetir_el_envio_escribe_la_misma_clave() -> None:
    cliente = ClienteFalso()
    archivo = ArchivoS3(cliente, "b", "AES256", 300)
    await archivo.guardar(CLAVE, CUERPO, METADATOS)
    await archivo.guardar(CLAVE, CUERPO, METADATOS)
    assert {llamada["Key"] for llamada in cliente.llamadas} == {CLAVE}


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "AccessDenied", "Message": CLAVE}}, "PutObject"),
        ClientError({"Error": {}}, "PutObject"),
        EndpointConnectionError(endpoint_url="http://interno"),
    ],
)
async def test_si_s3_falla_la_dependencia_no_esta_disponible_sin_filtrar_la_causa(
    error: Exception, capturar_logs: Any
) -> None:
    salida = capturar_logs()
    with pytest.raises(DependenciaNoDisponible) as excepcion:
        await ArchivoS3(ClienteFalso(error), "b", "AES256", 300).guardar(CLAVE, CUERPO, METADATOS)
    assert excepcion.value.__cause__ is None and excepcion.value.__suppress_context__
    texto = salida.getvalue()
    assert "error_type" in texto
    # Ni la clave del objeto, ni el cuerpo, ni el mensaje de AWS.
    assert CLAVE not in texto and "valor-sensible" not in texto and "interno" not in texto


async def test_pasado_el_corte_la_dependencia_no_esta_disponible() -> None:
    inicio = time.perf_counter()
    with pytest.raises(DependenciaNoDisponible):
        await ArchivoS3(ClienteFalso(demora_s=0.5), "b", "AES256", 50).guardar(
            CLAVE, CUERPO, METADATOS
        )
    assert time.perf_counter() - inicio < 0.4


async def test_sin_bucket_no_se_acepta_ningun_evento() -> None:
    with pytest.raises(DependenciaNoDisponible):
        await ArchivoSinConfigurar().guardar(CLAVE, CUERPO, METADATOS)


def test_el_cliente_no_reintenta_y_corta_a_los_milisegundos_configurados() -> None:
    """R-28: `total_max_attempts: 1` (con `max_attempts: 1` boto3 haría dos intentos)."""
    cliente = cliente_s3("us-east-1", 300, "http://localhost:4566")
    config = cliente.meta.config
    assert config.retries["total_max_attempts"] == 1
    assert (config.connect_timeout, config.read_timeout) == (0.3, 0.3)
    assert config.s3["addressing_style"] == "path"
    assert cliente_s3("us-east-1", 300).meta.config.s3 in (None, {})
