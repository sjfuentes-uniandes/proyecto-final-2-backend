"""El almacén S3 de los documentos de póliza (HU-W17, 17.7; ADR 0014, decisión 4) y su
configuración, sin red: `botocore.stub.Stubber` para `PutObject` y un cliente real (con credenciales
falsas) para la URL prefirmada, que se firma en local. LocalStack lo prueba
`tests/module/test_documento_w17.py`.
"""

import logging
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
from botocore.stub import Stubber
from polizas.adapters.outbound.documentos_s3 import (
    MENSAJE_SIN_BUCKET,
    MENSAJE_SIN_CLIENTE,
    AlmacenDocumentosS3,
    almacen_desde,
    cliente_s3,
)
from polizas.config import Settings
from polizas.domain.poliza.errores import FalloAlmacenDocumentos
from pydantic import ValidationError

pytestmark = pytest.mark.anyio
BUCKET = "solventa-int-polizas-123456789012"
POLIZA = UUID("6f1c2d3e-4b5a-4c6d-8e7f-001122334455")
CLAVE = f"polizas/{POLIZA}/100-2026-000001.pdf"
PDF = b"%PDF-1.3 sintetico"
VARIABLES = (
    "POLIZAS_BUCKET",
    "POLIZAS_BUCKET_SSE",
    "ADAPTADOR_FIRMA_URL",
    "DOCUMENTO_URL_VENCE_S",
    "AWS_REGION",
    "AWS_ENDPOINT_URL",
)


@pytest.fixture(autouse=True)
def entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in VARIABLES:
        monkeypatch.delenv(nombre, raising=False)
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)
    # Credenciales falsas: nada sale a la red (el Stubber responde y la URL se firma en local).
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDSINTETICA")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secreto-sintetico")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _cliente() -> Any:
    return cliente_s3("us-east-1")


# --- El cliente --------------------------------------------------------------------------------
def test_el_cliente_firma_con_sigv4_y_tiene_timeouts_y_reintentos_explicitos() -> None:
    cliente = cliente_s3("us-east-2")
    config = cliente.meta.config
    assert cliente.meta.region_name == "us-east-2"
    assert config.signature_version == "s3v4"
    assert (config.connect_timeout, config.read_timeout) == (1.0, 5.0)
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
    assert cliente.meta.endpoint_url == "https://s3.us-east-2.amazonaws.com"


@pytest.mark.parametrize("endpoint", ["http://localhost:4566", "http://localstack:4566"])
async def test_con_endpoint_local_la_url_usa_rutas(endpoint: str) -> None:
    # LocalStack (pruebas de módulo y compose.yaml): el bucket va en la ruta, no como subdominio,
    # que no resolvería (plan §b, decisión 12).
    cliente = cliente_s3("us-east-1", endpoint_url=endpoint)
    assert cliente.meta.endpoint_url == endpoint
    url = await AlmacenDocumentosS3(cliente, BUCKET, "AES256").url_descarga(CLAVE, "v-1", 300)
    assert url.startswith(f"{endpoint}/{BUCKET}/{CLAVE}?")


# --- guardar -----------------------------------------------------------------------------------
@pytest.mark.parametrize("cifrado", ["AES256", "aws:kms"])
async def test_guardar_envia_el_cifrado_del_bucket_y_el_tipo(cifrado: str) -> None:
    cliente = _cliente()
    # Los parámetros exactos: con aws:kms, sin SSEKMSKeyId (la política del bucket no lo exige).
    with Stubber(cliente) as stub:
        stub.add_response(
            "put_object",
            {"VersionId": "v-1", "ServerSideEncryption": cifrado},
            expected_params={
                "Bucket": BUCKET,
                "Key": CLAVE,
                "Body": PDF,
                "ContentType": "application/pdf",
                "ServerSideEncryption": cifrado,
            },
        )
        assert await AlmacenDocumentosS3(cliente, BUCKET, cifrado).guardar(CLAVE, PDF) == "v-1"
        stub.assert_no_pending_responses()


async def test_guardar_corre_en_un_hilo() -> None:
    hilos: list[int] = []

    class Cliente:
        def put_object(self, **_: Any) -> dict[str, Any]:
            hilos.append(threading.get_ident())
            return {"VersionId": "v-1"}

    assert await AlmacenDocumentosS3(Cliente(), BUCKET, "AES256").guardar(CLAVE, PDF) == "v-1"
    assert hilos and hilos[0] != threading.get_ident()


@pytest.mark.parametrize("respuesta", [{}, {"VersionId": "null"}, {"VersionId": ""}])
async def test_sin_version_el_bucket_no_esta_versionado(respuesta: dict[str, Any]) -> None:
    cliente = _cliente()
    with Stubber(cliente) as stub:
        stub.add_response("put_object", respuesta)
        with pytest.raises(FalloAlmacenDocumentos) as error:
            await AlmacenDocumentosS3(cliente, BUCKET, "AES256").guardar(CLAVE, PDF)
    assert (error.value.codigo, error.value.error_s3) == ("ALMACEN_SIN_VERSION", None)


@pytest.mark.parametrize(
    ("codigo_s3", "estado", "codigo"),
    [
        ("AccessDenied", 403, "ALMACEN_RECHAZADO"),
        ("NoSuchBucket", 404, "ALMACEN_RECHAZADO"),
        ("InternalError", 500, "ALMACEN_NO_DISPONIBLE"),
        ("SlowDown", 503, "ALMACEN_NO_DISPONIBLE"),
    ],
)
async def test_un_error_de_s3_es_un_fallo_con_codigo(
    codigo_s3: str, estado: int, codigo: str
) -> None:
    cliente = _cliente()
    with Stubber(cliente) as stub:
        stub.add_client_error(
            "put_object", codigo_s3, f"mensaje con {CLAVE}", http_status_code=estado
        )
        with pytest.raises(FalloAlmacenDocumentos) as error:
            await AlmacenDocumentosS3(cliente, BUCKET, "AES256").guardar(CLAVE, PDF)
    assert (error.value.codigo, error.value.error_s3) == (codigo, codigo_s3)
    # Ni la clave ni el mensaje de S3 en el error.
    assert CLAVE not in str(error.value)
    assert str(error.value) == codigo


async def test_sin_conexion_es_un_fallo_no_disponible() -> None:
    class Cliente:
        def put_object(self, **_: Any) -> dict[str, Any]:
            raise EndpointConnectionError(endpoint_url="https://s3.amazonaws.com")

    with pytest.raises(FalloAlmacenDocumentos) as error:
        await AlmacenDocumentosS3(Cliente(), BUCKET, "AES256").guardar(CLAVE, PDF)
    assert (error.value.codigo, error.value.error_s3) == ("ALMACEN_NO_DISPONIBLE", None)


# --- url_descarga ------------------------------------------------------------------------------
async def test_la_url_es_de_esa_version_como_adjunto_y_vence() -> None:
    url = await AlmacenDocumentosS3(_cliente(), BUCKET, "AES256").url_descarga(CLAVE, "v-1", 300)
    partes = urlsplit(url)
    consulta = {k: v[0] for k, v in parse_qs(partes.query).items()}
    assert (partes.scheme, partes.netloc) == ("https", f"{BUCKET}.s3.amazonaws.com")
    assert partes.path == f"/{CLAVE}"
    assert consulta["versionId"] == "v-1"
    assert consulta["response-content-disposition"] == 'attachment; filename="100-2026-000001.pdf"'
    assert consulta["X-Amz-Expires"] == "300"
    assert consulta["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert consulta["X-Amz-SignedHeaders"] == "host"
    assert set(consulta) == {
        "versionId",
        "response-content-disposition",
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
    }


@pytest.mark.parametrize("vence_s", [0, -1])
async def test_la_url_exige_un_vencimiento_positivo(vence_s: int) -> None:
    with pytest.raises(ValueError, match="vence_s"):
        await AlmacenDocumentosS3(_cliente(), BUCKET, "AES256").url_descarga(CLAVE, "v-1", vence_s)


async def test_sin_credenciales_la_url_es_un_fallo_no_disponible() -> None:
    class Cliente:
        def generate_presigned_url(self, *_: Any, **__: Any) -> str:
            raise NoCredentialsError()

    with pytest.raises(FalloAlmacenDocumentos) as error:
        await AlmacenDocumentosS3(Cliente(), BUCKET, "AES256").url_descarga(CLAVE, "v-1", 300)
    assert error.value.codigo == "ALMACEN_NO_DISPONIBLE"


# --- Configuración -----------------------------------------------------------------------------
def test_configuracion_por_omision() -> None:
    settings = Settings()
    assert settings.polizas_bucket is None
    assert settings.polizas_bucket_sse == "AES256"
    assert settings.documento_url_vence_s == 300
    assert settings.adaptador_firma_url == "http://localhost:8092"


def test_configuracion_con_los_nombres_de_ecs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLIZAS_BUCKET", BUCKET)
    monkeypatch.setenv("POLIZAS_BUCKET_SSE", "aws:kms")
    monkeypatch.setenv("ADAPTADOR_FIRMA_URL", "http://adaptador-firma:8080")
    settings = Settings()
    assert (settings.polizas_bucket, settings.polizas_bucket_sse) == (BUCKET, "aws:kms")
    assert settings.adaptador_firma_url == "http://adaptador-firma:8080"


def test_configuracion_con_el_prefijo_solventa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_POLIZAS_BUCKET", BUCKET)
    monkeypatch.setenv("SOLVENTA_POLIZAS_BUCKET_SSE", "AES256")
    monkeypatch.setenv("SOLVENTA_ADAPTADOR_FIRMA_URL", "http://localhost:9000")
    monkeypatch.setenv("SOLVENTA_DOCUMENTO_URL_VENCE_S", "900")
    settings = Settings()
    assert settings.polizas_bucket == BUCKET
    assert settings.adaptador_firma_url == "http://localhost:9000"
    assert settings.documento_url_vence_s == 900


@pytest.mark.parametrize(
    ("nombre", "valor"),
    [
        ("POLIZAS_BUCKET_SSE", "aws:kms:dsse"),
        ("POLIZAS_BUCKET_SSE", "aes256"),
        ("SOLVENTA_DOCUMENTO_URL_VENCE_S", "59"),
        ("SOLVENTA_DOCUMENTO_URL_VENCE_S", "3601"),
    ],
)
def test_configuracion_invalida_no_arranca(
    monkeypatch: pytest.MonkeyPatch, nombre: str, valor: str
) -> None:
    monkeypatch.setenv(nombre, valor)
    with pytest.raises(ValidationError):
        Settings()


def test_sin_bucket_no_hay_almacen_y_se_registra(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="polizas.documentos"):
        assert almacen_desde(Settings()) is None
    [registro] = caplog.records
    assert (registro.levelno, registro.getMessage()) == (logging.INFO, MENSAJE_SIN_BUCKET)
    assert registro.missing == "POLIZAS_BUCKET"  # type: ignore[attr-defined]


def test_con_bucket_el_almacen_usa_el_cifrado_y_el_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLIZAS_BUCKET", BUCKET)
    monkeypatch.setenv("POLIZAS_BUCKET_SSE", "aws:kms")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("SOLVENTA_AWS_ENDPOINT_URL", "http://localstack:4566")
    almacen = almacen_desde(Settings())
    assert isinstance(almacen, AlmacenDocumentosS3)
    assert (almacen.bucket, almacen.cifrado) == (BUCKET, "aws:kms")
    assert almacen.cliente.meta.region_name == "us-east-2"
    assert almacen.cliente.meta.endpoint_url == "http://localstack:4566"


def test_si_no_se_puede_crear_el_cliente_no_hay_almacen_y_se_registra(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("POLIZAS_BUCKET", BUCKET)

    def fallar(*_: Any, **__: Any) -> Any:
        raise ValueError(f"región inválida para {BUCKET}")

    monkeypatch.setattr(boto3, "client", fallar)
    with caplog.at_level(logging.INFO, logger="polizas.documentos"):
        assert almacen_desde(Settings()) is None
    [registro] = caplog.records
    assert (registro.levelno, registro.getMessage()) == (logging.ERROR, MENSAJE_SIN_CLIENTE)
    assert registro.error_type == "ValueError"  # type: ignore[attr-defined]
    assert BUCKET not in registro.getMessage()
