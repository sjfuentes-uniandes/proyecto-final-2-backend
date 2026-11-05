"""EvidenciasS3 contra S3 (LocalStack 4.14.0): integridad por checksum, URL prefirmada y cortes."""

import hashlib
import time
from typing import Any
from uuid import uuid4

import anyio
import httpx
import pytest
from siniestros.adapters.outbound.evidencias_s3 import EvidenciasS3, cliente_s3
from siniestros.domain.siniestro.modelos import IntegridadEvidencia

pytestmark = pytest.mark.anyio
REGION = "us-east-1"
SSE = "AES256"
CONTENIDO = b"\x89PNG-sintetico-de-una-foto"
SHA = hashlib.sha256(CONTENIDO).hexdigest()


def subir(s3: Any, bucket: str, contenido: bytes = CONTENIDO, **extra: Any) -> str:
    clave = f"siniestros/{uuid4()}/{uuid4()}"
    s3.put_object(Bucket=bucket, Key=clave, Body=contenido, ServerSideEncryption=SSE, **extra)
    return clave


def sha256_base64(contenido: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(contenido).digest()).decode()


@pytest.fixture
def almacen(localstack: str, bucket: str, credenciales: None) -> EvidenciasS3:
    cliente = cliente_s3(REGION, 2000, localstack)
    return EvidenciasS3(cliente, bucket, 2000)


async def test_un_objeto_con_su_checksum_es_verificada(
    s3: Any, bucket: str, almacen: EvidenciasS3
) -> None:
    clave = subir(s3, bucket, ChecksumSHA256=sha256_base64(CONTENIDO))
    assert await almacen.verificar(clave, SHA) is IntegridadEvidencia.VERIFICADA


async def test_un_objeto_con_otro_contenido_es_alterada(
    s3: Any, bucket: str, almacen: EvidenciasS3
) -> None:
    otro = b"contenido-alterado"
    clave = subir(s3, bucket, otro, ChecksumSHA256=sha256_base64(otro))
    assert await almacen.verificar(clave, SHA) is IntegridadEvidencia.ALTERADA


async def test_un_objeto_ausente_es_no_disponible(almacen: EvidenciasS3) -> None:
    assert await almacen.verificar("siniestros/no/existe", SHA) is IntegridadEvidencia.NO_DISPONIBLE


async def test_un_objeto_sin_checksum_sha256_es_no_verificable(
    s3: Any, bucket: str, almacen: EvidenciasS3
) -> None:
    # La carga de M01 debe exigir x-amz-checksum-sha256; sin él (aquí CRC32) no hay con qué comparar.
    clave = subir(s3, bucket, ChecksumAlgorithm="CRC32")
    assert await almacen.verificar(clave, SHA) is IntegridadEvidencia.NO_VERIFICABLE


async def test_un_bucket_inexistente_es_no_disponible(localstack: str, credenciales: None) -> None:
    almacen = EvidenciasS3(cliente_s3(REGION, 2000, localstack), "no-existe-este-bucket", 2000)
    assert await almacen.verificar("siniestros/x", SHA) is IntegridadEvidencia.NO_DISPONIBLE


async def test_un_servicio_que_no_responde_es_no_verificable_dentro_del_corte(
    credenciales: None,
) -> None:
    # Puerto cerrado: la conexión falla de inmediato (o el corte de 300 ms la cierra).
    almacen = EvidenciasS3(cliente_s3(REGION, 300, "http://127.0.0.1:9"), "bucket", 300)
    inicio = time.monotonic()
    assert await almacen.verificar("siniestros/x", SHA) is IntegridadEvidencia.NO_VERIFICABLE
    assert time.monotonic() - inicio < 1.5


async def test_la_url_prefirmada_descarga_el_objeto_y_expira_en_300_s(
    s3: Any, bucket: str, almacen: EvidenciasS3
) -> None:
    clave = subir(s3, bucket, ChecksumSHA256=sha256_base64(CONTENIDO))
    url = await almacen.firmar_lectura(clave, 300, "image/png")
    assert "X-Amz-Expires=300" in url
    async with httpx.AsyncClient() as http:
        respuesta = await http.get(url)
    assert respuesta.status_code == 200
    assert respuesta.content == CONTENIDO
    assert respuesta.headers["content-type"] == "image/png"


async def test_la_url_se_firma_con_el_endpoint_publico(
    s3: Any, localstack: str, bucket: str, credenciales: None
) -> None:
    clave = subir(s3, bucket)
    interno = cliente_s3(REGION, 2000, localstack)
    publico = cliente_s3(REGION, 2000, "http://localhost:4566")
    url = await EvidenciasS3(interno, bucket, 2000, publico).firmar_lectura(clave, 60, "image/png")
    assert url.startswith(f"http://localhost:4566/{bucket}/")
    assert "X-Amz-Expires=60" in url


async def test_mt_w19_01_el_enlace_vencido_ya_no_descarga_el_objeto(
    s3: Any, bucket: str, almacen: EvidenciasS3
) -> None:
    """MT-W19-01: tras la caducidad S3 rechaza la URL (403); el analista pide un enlace nuevo."""
    clave = subir(s3, bucket, ChecksumSHA256=sha256_base64(CONTENIDO))
    url = await almacen.firmar_lectura(clave, 1, "image/png")
    async with httpx.AsyncClient() as http:
        assert (await http.get(url)).status_code == 200
        await anyio.sleep(2.5)
        vencido = await http.get(url)
        assert vencido.status_code == 403
        assert "expired" in vencido.text.lower()
        # Un enlace nuevo del mismo objeto sí descarga: el vencimiento es del enlace, no del archivo.
        nuevo = await almacen.firmar_lectura(clave, 300, "image/png")
        assert (await http.get(nuevo)).content == CONTENIDO
