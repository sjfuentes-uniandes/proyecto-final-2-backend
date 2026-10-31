"""MT-W17-07 (17.7): el documento de la póliza contra S3 y PostgreSQL reales.

- LocalStack 4.14.0 con S3 (y KMS), `S3_SKIP_SIGNATURE_VALIDATION=0` y un bucket versionado por
  prueba: `guardar` devuelve un `versionId` y el objeto queda con el cifrado pedido; dos `guardar`
  de la misma clave son dos versiones; la URL de una versión descarga esos bytes, como adjunto; una
  URL vencida o alterada responde 403. La política del bucket (`DenyUnencryptedWrites`) no se prueba
  aquí: LocalStack no la aplica (exploración §E). Lo que se prueba es que el objeto queda cifrado
  como se pidió; con `aws:kms` eso solo puede venir del encabezado, porque el cifrado por omisión de
  S3 es `AES256`.
- PostgreSQL: los datos del PDF de una póliza EMITIENDO creada desde `RecaudoConfirmado`, en una
  transacción, y el PDF que sale de ellos.
"""

import asyncio
import dataclasses
import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from polizas.adapters.outbound.documento_pdf import GeneradorPdf
from polizas.adapters.outbound.documentos_s3 import AlmacenDocumentosS3, cliente_s3
from polizas.adapters.outbound.polizas_pg import PolizasPg
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.application.poliza.documento import ReunirDatosDocumento
from polizas.application.recaudo.proyectar import ProyectarRecaudo
from polizas.domain.poliza.documento import DatosDocumento, clave_documento
from polizas.domain.poliza.errores import FalloAlmacenDocumentos
from polizas.domain.poliza.modelos import EstadoPoliza, Producto, Ramo, Vigencia
from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo
from polizas.domain.suscripcion.modelos import Cobertura
from solventa_common.inbox import InboxPg

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.anyio
REGION = "us-east-1"
ASESOR = {"X-Actor-Id": "sub-asesor-1", "X-Actor-Rol": "ASESOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
CONFIRMADO_EN = datetime(2026, 10, 22, 15, 0, 1, tzinfo=UTC)
FECHA_DOCUMENTO = datetime(2026, 10, 22, 15, 0, 2, tzinfo=UTC)
DATOS = DatosDocumento(
    numero="100-2026-000001",
    ramo=Ramo("100", "Vida hipotecario"),
    producto=Producto("vida-hipotecario", "2026.09.1"),
    vigencia=Vigencia(date(2026, 10, 23), date(2027, 10, 22)),
    coberturas=(Cobertura("MUERTE", "Muerte por cualquier causa", True),),
    suma_asegurada=Decimal(180000000),
    prima=Decimal(64638),
    periodicidad="MENSUAL",
    moneda="COP",
    cliente_id=UUID("00000000-0000-4000-8000-000000000001"),
    socio_id=None,
    aceptada_en=datetime(2026, 10, 22, 15, 0, tzinfo=UTC),
    transaccion_id="TX-SINTETICA-0001",
    version_reglas_prima="2026.09.1",
    version_reglas_suscripcion="2026.10.1",
    fecha_documento=FECHA_DOCUMENTO,
)


def _sha256(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


def _pdf(**cambios: Any) -> bytes:
    return GeneradorPdf().generar(dataclasses.replace(DATOS, **cambios))


def _clave() -> str:
    return f"polizas/{uuid4()}/{DATOS.numero}.pdf"


@pytest.fixture
def s3(localstack_s3: str, credenciales: None) -> Any:
    return cliente_s3(REGION, endpoint_url=localstack_s3)


def _bucket(s3: Any, *, versionado: bool = True) -> str:
    nombre = f"polizas-{uuid4().hex[:16]}"
    s3.create_bucket(Bucket=nombre)
    if versionado:
        s3.put_bucket_versioning(Bucket=nombre, VersioningConfiguration={"Status": "Enabled"})
    return nombre


async def _descargar(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as cliente:
        return await cliente.get(url)


def _con_parametro(url: str, nombre: str, valor: str) -> str:
    partes = urlsplit(url)
    consulta = dict(parse_qsl(partes.query, keep_blank_values=True))
    consulta[nombre] = valor
    return urlunsplit(partes._replace(query=urlencode(consulta)))


# --- S3 ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("cifrado", ["AES256", "aws:kms"])
async def test_guardar_devuelve_la_version_y_el_objeto_queda_cifrado(s3: Any, cifrado: str) -> None:
    bucket, clave, pdf = _bucket(s3), _clave(), _pdf()
    version = await AlmacenDocumentosS3(s3, bucket, cifrado).guardar(clave, pdf)
    assert version and version != "null"
    cabecera = await asyncio.to_thread(s3.head_object, Bucket=bucket, Key=clave, VersionId=version)
    assert cabecera["ServerSideEncryption"] == cifrado
    assert cabecera["ContentType"] == "application/pdf"
    assert cabecera["ContentLength"] == len(pdf)
    assert cabecera["VersionId"] == version
    # Sin SSEKMSKeyId, LocalStack cifra con una clave suya (en AWS sería aws/s3: ver el reporte).
    assert ("SSEKMSKeyId" in cabecera) is (cifrado == "aws:kms")


async def test_dos_guardar_de_la_misma_clave_son_dos_versiones(s3: Any) -> None:
    bucket, clave = _bucket(s3), _clave()
    almacen = AlmacenDocumentosS3(s3, bucket, "AES256")
    primera = await almacen.guardar(clave, _pdf())
    segunda = await almacen.guardar(clave, _pdf(numero="100-2026-000002"))
    assert primera != segunda
    versiones = await asyncio.to_thread(s3.list_object_versions, Bucket=bucket, Prefix=clave)
    assert sorted(v["VersionId"] for v in versiones["Versions"]) == sorted([primera, segunda])
    assert {v["Key"] for v in versiones["Versions"]} == {clave}


async def test_la_url_de_una_version_descarga_esos_bytes_como_adjunto(s3: Any) -> None:
    bucket, clave = _bucket(s3), _clave()
    almacen = AlmacenDocumentosS3(s3, bucket, "AES256")
    pdf_1, pdf_2 = _pdf(), _pdf(prima=Decimal(70000))
    assert _sha256(pdf_1) != _sha256(pdf_2)
    version_1 = await almacen.guardar(clave, pdf_1)
    version_2 = await almacen.guardar(clave, pdf_2)
    # La primera versión ya no es la vigente: la URL debe traer esa, no la última.
    for version, pdf in ((version_1, pdf_1), (version_2, pdf_2)):
        respuesta = await _descargar(await almacen.url_descarga(clave, version, 300))
        assert respuesta.status_code == 200, respuesta.text
        assert _sha256(respuesta.content) == _sha256(pdf)
        assert respuesta.headers["x-amz-version-id"] == version
        assert respuesta.headers["content-type"] == "application/pdf"
        assert respuesta.headers["content-disposition"] == (
            f'attachment; filename="{DATOS.numero}.pdf"'
        )


async def test_una_url_vencida_o_alterada_responde_403(s3: Any) -> None:
    bucket, clave = _bucket(s3), _clave()
    almacen = AlmacenDocumentosS3(s3, bucket, "AES256")
    version_1 = await almacen.guardar(clave, _pdf())
    version_2 = await almacen.guardar(clave, _pdf(numero="100-2026-000002"))
    url = await almacen.url_descarga(clave, version_1, 300)
    assert (await _descargar(url)).status_code == 200

    firma = dict(parse_qsl(urlsplit(url).query))["X-Amz-Signature"]
    otra_firma = firma[:-1] + ("0" if firma[-1] != "0" else "1")
    alterada = await _descargar(_con_parametro(url, "X-Amz-Signature", otra_firma))
    assert alterada.status_code == 403
    assert "SignatureDoesNotMatch" in alterada.text
    # Cambiar la versión invalida la firma: la URL no sirve para otra versión.
    otra_version = await _descargar(_con_parametro(url, "versionId", version_2))
    assert otra_version.status_code == 403
    assert "SignatureDoesNotMatch" in otra_version.text

    corta = await almacen.url_descarga(clave, version_1, 1)
    await asyncio.sleep(3)
    vencida = await _descargar(corta)
    assert vencida.status_code == 403
    assert "Request has expired" in vencida.text


async def test_un_bucket_sin_versionado_es_un_fallo(s3: Any) -> None:
    almacen = AlmacenDocumentosS3(s3, _bucket(s3, versionado=False), "AES256")
    with pytest.raises(FalloAlmacenDocumentos) as error:
        await almacen.guardar(_clave(), _pdf())
    assert error.value.codigo == "ALMACEN_SIN_VERSION"


async def test_un_bucket_inexistente_es_un_rechazo(s3: Any) -> None:
    almacen = AlmacenDocumentosS3(s3, f"polizas-{uuid4().hex[:16]}", "AES256")
    with pytest.raises(FalloAlmacenDocumentos) as error:
        await almacen.guardar(_clave(), _pdf())
    assert (error.value.codigo, error.value.error_s3) == ("ALMACEN_RECHAZADO", "NoSuchBucket")


# --- PostgreSQL --------------------------------------------------------------------------------
async def _aceptar(api: Any, sinteticos: Any) -> UUID:
    """Suscripción y aceptación de S01 por la API de polizas; devuelve la clave (= su id)."""
    decision = await api.post(
        "/v1/suscripciones",
        json={"cotizacionId": sinteticos.cotizacion_id("S01")},
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert decision.status_code == 201, decision.json()
    cuerpo = decision.json()
    aceptacion = await api.post(
        f"/v1/cotizaciones/{cuerpo['cotizacionId']}/aceptacion",
        json={
            "decisionId": cuerpo["id"],
            "versionDeclaraciones": "2026.10.1",
            "declaraciones": TODAS,
        },
        headers={**ASESOR, "Idempotency-Key": str(uuid4())},
    )
    assert aceptacion.status_code == 201, aceptacion.json()
    return UUID(aceptacion.json()["id"])


def _resultado(clave: UUID, estado: SituacionRecaudo) -> ResultadoRecaudo:
    confirmado = estado is SituacionRecaudo.CONFIRMADO
    return ResultadoRecaudo(
        clave_recaudo=clave,
        recaudo_id=uuid4(),
        estado=estado,
        monto=Decimal(64638) if confirmado else None,
        moneda="COP" if confirmado else None,
        transaccion_id="TX-SINTETICA-0001" if confirmado else None,
        motivo=None if confirmado else "FONDOS_INSUFICIENTES",
        ocurrido_en=CONFIRMADO_EN,
    )


async def test_los_datos_del_pdf_salen_de_una_poliza_emitiendo_real(
    api: Any, sinteticos: Any, limpia: str, dobles: Any
) -> None:
    from psycopg_pool import AsyncConnectionPool

    clave = await _aceptar(api, sinteticos)
    with psycopg.connect(limpia) as conexion:
        fila = conexion.execute(
            "SELECT a.aceptada_en, d.version_reglas FROM aceptaciones a "
            "JOIN decisiones_suscripcion d ON d.id = a.decision_id WHERE a.id = %s",
            (clave,),
        ).fetchone()
    assert fila is not None
    aceptada_en, version_reglas = fila
    insumos = sinteticos.datos["casos"][0]["insumos"]
    assert sinteticos.datos["casos"][0]["id"] == "S01"

    async with AsyncConnectionPool(limpia, min_size=1, max_size=2, open=False) as pool:
        inbox, polizas = InboxPg(pool), PolizasPg()
        caso = ProyectarRecaudo(RecaudosPg(pool), polizas, dobles.reloj)
        async with inbox.transaccion() as tx:
            await caso.ejecutar(_resultado(clave, SituacionRecaudo.CONFIRMADO), tx)
        async with inbox.transaccion() as tx:
            poliza = await polizas.por_aceptacion(clave, tx)
            assert poliza is not None and poliza.estado is EstadoPoliza.EMITIENDO
            datos = await ReunirDatosDocumento(polizas).ejecutar(poliza, FECHA_DOCUMENTO, tx)

    assert datos == DatosDocumento(
        numero=poliza.numero,
        ramo=Ramo("100", "Vida hipotecario"),
        producto=Producto("vida-hipotecario", insumos["versionProducto"]),
        vigencia=poliza.vigencia,
        coberturas=tuple(
            Cobertura(c["codigo"], c["nombre"], c["obligatoria"]) for c in insumos["coberturas"]
        ),
        suma_asegurada=Decimal(insumos["sumaAsegurada"]),
        prima=Decimal(insumos["primaTotal"]),
        periodicidad=insumos["periodicidad"],
        moneda=insumos["moneda"],
        cliente_id=UUID(insumos["clienteId"]),
        socio_id=insumos["socioId"],
        aceptada_en=aceptada_en,
        transaccion_id="TX-SINTETICA-0001",
        version_reglas_prima=insumos["versionReglasPrima"],
        version_reglas_suscripcion=version_reglas,
        fecha_documento=FECHA_DOCUMENTO,
    )
    # Aceptada el 22/10/2026 en Bogotá (reloj de los dobles).
    assert datos.vigencia == Vigencia(date(2026, 10, 23), date(2027, 10, 22))
    assert clave_documento(poliza) == f"polizas/{poliza.poliza_id}/{poliza.numero}.pdf"
    pdf = GeneradorPdf().generar(datos)
    assert f"N.\\272 {poliza.numero}".encode() in pdf
    assert pdf == GeneradorPdf().generar(datos)


async def test_sin_recaudo_confirmado_no_hay_fuente_del_documento(
    api: Any, sinteticos: Any, limpia: str, dobles: Any
) -> None:
    from psycopg_pool import AsyncConnectionPool

    clave = await _aceptar(api, sinteticos)
    async with AsyncConnectionPool(limpia, min_size=1, max_size=2, open=False) as pool:
        inbox, polizas = InboxPg(pool), PolizasPg()
        async with inbox.transaccion() as tx:
            assert await polizas.fuente_documento(clave, tx) is None
            assert await polizas.fuente_documento(uuid4(), tx) is None
        caso = ProyectarRecaudo(RecaudosPg(pool), polizas, dobles.reloj)
        async with inbox.transaccion() as tx:
            await caso.ejecutar(_resultado(clave, SituacionRecaudo.RECHAZADO), tx)
        async with inbox.transaccion() as tx:
            assert await polizas.fuente_documento(clave, tx) is None
            assert await polizas.por_aceptacion(clave, tx) is None
