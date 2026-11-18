"""EvidenciasS3 con un cliente falso (sin red). El S3 real (LocalStack) lo prueban las de módulo."""

import base64
import hashlib
import logging
import time
from typing import Any
from unittest.mock import Mock

import pytest
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError
from siniestros.adapters.outbound.evidencias_s3 import (
    AlmacenSinConfigurar,
    EvidenciasS3,
    checksum_base64,
    cliente_s3,
)
from siniestros.application.siniestro.errores import DependenciaNoDisponible
from siniestros.domain.siniestro.modelos import IntegridadEvidencia

pytestmark = pytest.mark.anyio
CLAVE = "siniestros/5e000000-0000-4000-8000-000004000000/e0000000-0000-4000-8000-000004000001"
CONTENIDO = b"contenido sintetico"
SHA = hashlib.sha256(CONTENIDO).hexdigest()


def error_cliente(codigo: str) -> ClientError:
    return ClientError({"Error": {"Code": codigo, "Message": "no mostrar"}}, "HeadObject")


class ClienteFalso:
    def __init__(self, respuesta: Any = None, demora_s: float = 0) -> None:
        self.respuesta = respuesta
        self.demora_s = demora_s
        self.llamadas: list[dict[str, Any]] = []
        self.firmas: list[tuple[str, dict[str, Any], int]] = []
        self.firmas_carga: list[dict[str, Any]] = []
        self.falla_firma: Exception | None = None

    def head_object(self, **argumentos: Any) -> dict[str, Any]:
        self.llamadas.append(argumentos)
        time.sleep(self.demora_s)
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return dict(self.respuesta)

    def generate_presigned_url(self, operacion: str, Params: dict[str, Any], ExpiresIn: int) -> str:
        if self.falla_firma:
            raise self.falla_firma
        self.firmas.append((operacion, Params, ExpiresIn))
        return "https://s3.local/bucket/objeto?X-Amz-Signature=firma"

    def generate_presigned_post(self, **argumentos: Any) -> dict[str, Any]:
        self.firmas_carga.append(argumentos)
        return {
            "url": "https://s3.local/bucket",
            "fields": argumentos["Fields"],
        }


def almacen(
    cliente: ClienteFalso, timeout_ms: int = 300, firma: ClienteFalso | None = None
) -> EvidenciasS3:
    return EvidenciasS3(cliente, "bucket-evidencias", timeout_ms, firma)


def test_el_checksum_es_el_base64_del_digest_binario() -> None:
    assert checksum_base64(SHA) == base64.b64encode(hashlib.sha256(CONTENIDO).digest()).decode()


async def test_el_objeto_integro_es_verificada_y_se_pide_el_checksum() -> None:
    cliente = ClienteFalso({"ChecksumSHA256": checksum_base64(SHA)})
    assert await almacen(cliente).verificar(CLAVE, SHA) is IntegridadEvidencia.VERIFICADA
    assert cliente.llamadas == [
        {"Bucket": "bucket-evidencias", "Key": CLAVE, "ChecksumMode": "ENABLED"}
    ]


async def test_conservar_tiene_su_corte_sin_ampliar_el_del_detalle() -> None:
    cliente = ClienteFalso(
        {
            "ChecksumSHA256": checksum_base64(SHA),
            "ContentLength": len(CONTENIDO),
            "ContentType": "image/png",
        },
        demora_s=0.4,
    )
    s3 = almacen(cliente)
    await s3.conservar("tmp/origen", CLAVE, SHA, len(CONTENIDO), "image/png", "AES256")
    assert await s3.verificar(CLAVE, SHA) is IntegridadEvidencia.NO_VERIFICABLE
    with pytest.raises(DependenciaNoDisponible):
        await EvidenciasS3(cliente, "bucket-evidencias", 300, conservacion_timeout_ms=50).conservar(
            "tmp/origen", CLAVE, SHA, len(CONTENIDO), "image/png", "AES256"
        )


@pytest.mark.parametrize("final_denegado", [False, True])
async def test_destino_sin_listbucket_exige_origen_y_copia_verificados(
    final_denegado: bool,
) -> None:
    cliente = Mock()
    datos = {
        "ChecksumSHA256": checksum_base64(SHA),
        "ContentLength": len(CONTENIDO),
        "ContentType": "image/png",
        "ETag": '"etag"',
        "VersionId": "version-origen",
    }
    cliente.head_object.side_effect = [
        error_cliente("403"),
        datos,
        error_cliente("403") if final_denegado else datos,
    ]
    s3 = EvidenciasS3(cliente, "bucket-evidencias", 300)
    if final_denegado:
        with pytest.raises(DependenciaNoDisponible):
            await s3.conservar("tmp/origen", CLAVE, SHA, len(CONTENIDO), "image/png", "AES256")
    else:
        await s3.conservar("tmp/origen", CLAVE, SHA, len(CONTENIDO), "image/png", "AES256")
    cliente.copy_object.assert_called_once_with(
        Bucket="bucket-evidencias",
        Key=CLAVE,
        CopySource={
            "Bucket": "bucket-evidencias",
            "Key": "tmp/origen",
            "VersionId": "version-origen",
        },
        CopySourceIfMatch='"etag"',
        ChecksumAlgorithm="SHA256",
        ServerSideEncryption="AES256",
        MetadataDirective="COPY",
    )


async def test_destino_y_origen_denegados_no_permiten_copiar() -> None:
    cliente = Mock()
    cliente.head_object.side_effect = error_cliente("403")
    with pytest.raises(DependenciaNoDisponible):
        await EvidenciasS3(cliente, "bucket-evidencias", 300).conservar(
            "tmp/origen", CLAVE, SHA, len(CONTENIDO), "image/png", "AES256"
        )
    cliente.copy_object.assert_not_called()


async def test_un_checksum_distinto_es_alterada() -> None:
    cliente = ClienteFalso({"ChecksumSHA256": checksum_base64("b" * 64)})
    assert await almacen(cliente).verificar(CLAVE, SHA) is IntegridadEvidencia.ALTERADA


@pytest.mark.parametrize("respuesta", [{}, {"ChecksumSHA256": ""}, {"ContentLength": 3}])
async def test_un_objeto_sin_checksum_es_no_verificable(respuesta: dict[str, Any]) -> None:
    assert (
        await almacen(ClienteFalso(respuesta)).verificar(CLAVE, SHA)
        is IntegridadEvidencia.NO_VERIFICABLE
    )


@pytest.mark.parametrize("codigo", ["404", "NoSuchKey", "NotFound"])
async def test_un_objeto_ausente_es_no_disponible(codigo: str) -> None:
    cliente = ClienteFalso(error_cliente(codigo))
    assert await almacen(cliente).verificar(CLAVE, SHA) is IntegridadEvidencia.NO_DISPONIBLE


@pytest.mark.parametrize(
    "falla",
    [error_cliente("403"), error_cliente("InternalError"), ConnectTimeoutError(endpoint_url="x")],
)
async def test_otros_fallos_son_no_verificable_y_no_se_registra_la_clave(
    falla: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        resultado = await almacen(ClienteFalso(falla)).verificar(CLAVE, SHA)
    assert resultado is IntegridadEvidencia.NO_VERIFICABLE
    texto = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert CLAVE not in texto
    assert "no mostrar" not in texto


async def test_pasado_el_corte_es_no_verificable() -> None:
    cliente = ClienteFalso({"ChecksumSHA256": checksum_base64(SHA)}, demora_s=0.3)
    inicio = time.monotonic()
    resultado = await almacen(cliente, timeout_ms=50).verificar(CLAVE, SHA)
    assert resultado is IntegridadEvidencia.NO_VERIFICABLE
    assert time.monotonic() - inicio < 0.25


async def test_firma_con_el_ttl_el_tipo_y_el_cliente_de_firma() -> None:
    lectura, firma = ClienteFalso(), ClienteFalso()
    url = await almacen(lectura, firma=firma).firmar_lectura(CLAVE, 300, "image/jpeg")
    assert "X-Amz-Signature" in url
    assert lectura.firmas == []
    assert firma.firmas == [
        (
            "get_object",
            {"Bucket": "bucket-evidencias", "Key": CLAVE, "ResponseContentType": "image/jpeg"},
            300,
        )
    ]


async def test_firma_post_fija_tipo_tamano_hash_cifrado_y_vencimiento() -> None:
    cliente = ClienteFalso()
    evidencia = almacen(cliente)

    url, campos = await evidencia.firmar_carga(
        "tmp/borrador/evidencia",
        "image/jpeg",
        1234,
        SHA,
        300,
        "AES256",
    )

    assert url == "https://s3.local/bucket"
    assert cliente.firmas_carga == [
        {
            "Bucket": "bucket-evidencias",
            "Key": "tmp/borrador/evidencia",
            "Fields": {
                "key": "tmp/borrador/evidencia",
                "Content-Type": "image/jpeg",
                "x-amz-checksum-algorithm": "SHA256",
                "x-amz-checksum-sha256": checksum_base64(SHA),
                "x-amz-server-side-encryption": "AES256",
            },
            "Conditions": [
                {"key": "tmp/borrador/evidencia"},
                {"Content-Type": "image/jpeg"},
                {"x-amz-checksum-algorithm": "SHA256"},
                {"x-amz-checksum-sha256": checksum_base64(SHA)},
                {"x-amz-server-side-encryption": "AES256"},
                ["content-length-range", 1234, 1234],
            ],
            "ExpiresIn": 300,
        }
    ]
    assert campos["x-amz-checksum-sha256"] == checksum_base64(SHA)


async def test_sin_cliente_de_firma_firma_con_el_de_lectura() -> None:
    cliente = ClienteFalso()
    await almacen(cliente).firmar_lectura(CLAVE, 60, "application/pdf")
    assert len(cliente.firmas) == 1


@pytest.mark.parametrize("falla", [error_cliente("AccessDenied"), BotoCoreError()])
async def test_si_no_puede_firmar_es_dependencia_no_disponible(falla: Exception) -> None:
    cliente = ClienteFalso()
    cliente.falla_firma = falla
    with pytest.raises(DependenciaNoDisponible) as error:
        await almacen(cliente).firmar_lectura(CLAVE, 300, "image/jpeg")
    assert error.value.detalles == {"dependencia": "evidencias"}


async def test_el_almacen_sin_configurar_no_verifica_ni_firma() -> None:
    sin = AlmacenSinConfigurar()
    assert await sin.verificar(CLAVE, SHA) is IntegridadEvidencia.NO_VERIFICABLE
    with pytest.raises(DependenciaNoDisponible):
        await sin.firmar_lectura(CLAVE, 300, "image/jpeg")


def test_el_cliente_real_tiene_corte_sin_reintentos_y_endpoint_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    cliente = cliente_s3("us-east-1", 300, "http://localhost:4566")
    config = cliente.meta.config
    assert (config.connect_timeout, config.read_timeout) == (0.3, 0.3)
    assert config.retries["total_max_attempts"] == 1
    assert config.s3 == {"addressing_style": "path"}
    assert cliente.meta.endpoint_url == "http://localhost:4566"
    en_aws = cliente_s3("us-east-1", 300)
    assert not (en_aws.meta.config.s3 or {}).get("addressing_style")
