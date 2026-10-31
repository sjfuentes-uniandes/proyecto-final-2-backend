"""MT-W17-11 (17.11): del portal a la póliza y la URL de su documento, con `polizas` real.

Cadena: `bff-web` -> `polizas` (uvicorn + PostgreSQL de Testcontainers, con sus migraciones), con su
consumidor de `polizas-recaudos` y su tarea emisora reales, contra un LocalStack 4.14.0 con SQS y S3
(`S3_SKIP_SIGNATURE_VALIDATION=0`, bucket versionado). Como la 16.14, se simulan las dependencias de
polizas que no son el objeto de la prueba: cotizacion y catalogo (insumos sintéticos y semilla de
reglas 2026.10.1), su reloj (fijado en el `ahoraDeReferencia`) y adaptador-firma, con las
respuestas de su contrato (`FirmaDto` o 503 `FUENTE_NO_DISPONIBLE`).

La suscripción y la aceptación se registran por la API de polizas; el `RecaudoConfirmado` llega por
la cola. Con la firma caída, la póliza queda EMITIENDO con su documento subido: 200 y el documento
409. Con la firma, ACTIVA: 200 y la URL del portal descarga el PDF, cuyo sha256 es la huella. Sin
recaudo confirmado, 404; con polizas caído, 503. Solo datos sintéticos.
"""

import asyncio
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from bff_web.adapters.inbound import polizas as ruta
from bff_web.config import settings
from bff_web.main import app as portal_app
from fastapi.testclient import TestClient
from polizas.adapters.outbound.aceptaciones_pg import AceptacionesPg
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.codigos import CodigosPg
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.decisiones_pg import DecisionesPg, migrar
from polizas.adapters.outbound.polizas_pg import PolizasLecturaPg
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.config import Settings as AjustesPolizas
from polizas.domain.suscripcion.insumos import huella
from polizas.main import Puertos
from polizas.main import crear_app as crear_polizas
from psycopg.rows import dict_row
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

docker = pytest.importorskip("docker")

RAIZ = Path(__file__).resolve().parents[4]
DATOS = json.loads(
    (RAIZ / "contracts/datos-sinteticos/cotizaciones-suscripcion.json").read_text(encoding="utf-8")
)
SEMILLA_REGLAS = (
    RAIZ
    / "services/catalogo/src/catalogo/adapters/outbound/semillas/reglas_suscripcion_2026_10_1.json"
)
S01 = next(c for c in DATOS["casos"] if c["id"] == "S01")["insumos"]
IMAGEN_LOCALSTACK = "localstack/localstack:4.14.0"
REGION = "us-east-1"
BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
}
ACTOR_POLIZAS = {"X-Actor-Id": "sub-asesora", "X-Actor-Rol": "ASESOR"}
TODAS = {
    "leidoCondiciones": True,
    "informacionVeraz": True,
    "debitoAutomatico": True,
    "avisosElectronicos": True,
}
RESUELTO_EN = "2026-10-22T15:00:01.250000Z"
TABLAS = "polizas, aceptaciones, decisiones_suscripcion, outbox, inbox, recaudos_aceptacion"


class Servidor:
    """Una app ASGI servida por uvicorn en un hilo propio (como la 16.14).

    psycopg asíncrono no corre sobre el ProactorEventLoop de Windows: se usa un
    SelectorEventLoop propio (en Linux es el bucle por defecto).
    """

    def __init__(self, app: Any) -> None:
        self._zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._zocalo.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._zocalo.getsockname()[1]}"
        self._servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self._hilo = threading.Thread(target=self._correr, daemon=True)

    def _correr(self) -> None:
        asyncio.run(
            self._servidor.serve(sockets=[self._zocalo]), loop_factory=asyncio.SelectorEventLoop
        )

    def arrancar(self) -> "Servidor":
        self._hilo.start()
        limite = time.monotonic() + 30
        while not self._servidor.started:
            assert self._hilo.is_alive(), "el servidor no arrancó (ver su log)"
            assert time.monotonic() < limite, "el servidor no arrancó a tiempo"
            time.sleep(0.05)
        return self

    def apagar(self) -> None:
        self._servidor.should_exit = True
        self._hilo.join(timeout=15)


class RelojFijo:
    """El `ahoraDeReferencia` de los datos sintéticos: la oferta de S01 está vigente."""

    def ahora(self) -> datetime:
        return datetime.fromisoformat(DATOS["ahoraDeReferencia"])


class Firma:
    """adaptador-firma con su contrato (17.4): `FirmaDto` del mismo documento y la misma huella, o
    503 FUENTE_NO_DISPONIBLE si está `caida`."""

    def __init__(self) -> None:
        self.caida = False
        self.url = ""

    async def firmar(self, pedido: Request) -> JSONResponse:
        if self.caida:
            return JSONResponse(
                {"codigo": "FUENTE_NO_DISPONIBLE", "mensaje": "m", "detalles": {}}, 503
            )
        cuerpo = await pedido.json()
        return JSONResponse(
            {
                "documentoId": cuerpo["documentoId"],
                "estado": "FIRMADO",
                "firmaId": "FIR-SINT-C01",
                "selloTiempo": "2026-01-01T00:00:00Z",
                "huella": cuerpo["huella"],
            }
        )


def _proveedores() -> httpx.MockTransport:
    """cotizacion y catalogo de polizas, como la 16.14."""
    insumos = {c["insumos"]["cotizacionId"]: c["insumos"] for c in DATOS["casos"]}
    definicion = json.loads(SEMILLA_REGLAS.read_text(encoding="utf-8"))
    reglas = {
        "productoId": definicion["productoId"],
        "version": DATOS["reglas"]["version"],
        "vigenteDesde": "2026-10-01T00:00:00Z",
        "hash": huella(definicion),
        "definicion": definicion,
    }

    def responder(pedido: httpx.Request) -> httpx.Response:
        if pedido.url.host == "catalogo":
            return httpx.Response(200, json=reglas)
        cotizacion_id = pedido.url.path.split("/")[3]
        if cotizacion_id not in insumos:
            return httpx.Response(404, json={"codigo": "COTIZACION_NO_ENCONTRADA"})
        return httpx.Response(200, json=insumos[cotizacion_id])

    return httpx.MockTransport(responder)


def _polizas(ajustes: AjustesPolizas) -> Starlette:
    """polizas con sus adaptadores de producción (PostgreSQL, SQS y S3), el consumidor y la tarea
    emisora, y el reloj fijo. El pool y la app nacen en el bucle de uvicorn."""
    estado: dict[str, Any] = {}

    @asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        from psycopg_pool import AsyncConnectionPool

        url = ajustes.database_url
        reloj = RelojFijo()
        async with (
            AsyncConnectionPool(url, min_size=1, max_size=5, open=False) as pool,
            httpx.AsyncClient(transport=_proveedores()) as salida,
        ):
            puertos = Puertos(
                insumos=CotizacionHttp(salida, "http://cotizacion", ajustes.cotizacion_timeout_ms),
                reglas=CatalogoHttp(salida, "http://catalogo", ajustes.catalogo_timeout_ms, reloj),
                decisiones=DecisionesPg(pool),
                codigos=CodigosPg(pool),
                reloj=reloj,
                aceptaciones=AceptacionesPg(pool),
                recaudos=RecaudosPg(pool),
                polizas=PolizasLecturaPg(pool),
            )
            app = crear_polizas(ajustes, puertos=puertos, pool=pool)
            async with app.router.lifespan_context(app):
                estado["app"] = app
                yield

    async def reenviar(scope: Scope, receive: Receive, send: Send) -> None:
        await estado["app"](scope, receive, send)

    return Starlette(lifespan=ciclo, routes=[Mount("/", app=reenviar)])


# --- Infraestructura (una por módulo) -----------------------------------------------------------


def _docker_disponible() -> None:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver=None) as contenedor:
        url = contenedor.get_connection_url()
        migrar(url)
        yield url


@pytest.fixture(scope="module")
def localstack() -> Iterator[str]:
    _docker_disponible()
    from testcontainers.community.localstack import LocalStackContainer

    contenedor = (
        LocalStackContainer(image=IMAGEN_LOCALSTACK, region_name=REGION)
        .with_services("sqs", "s3")
        .with_env("S3_SKIP_SIGNATURE_VALIDATION", "0")
    )
    with contenedor:
        yield contenedor.get_url()


@pytest.fixture(scope="module")
def aws(localstack: str) -> Iterator[dict[str, Any]]:
    """Credenciales de LocalStack para boto3 (las de polizas y las de la prueba), un bucket
    versionado y la cola `polizas-recaudos`."""
    import boto3

    previas = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}
    os.environ.update(AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test")
    s3 = boto3.client("s3", region_name=REGION, endpoint_url=localstack)
    sqs = boto3.client("sqs", region_name=REGION, endpoint_url=localstack)
    bucket = f"polizas-{uuid4().hex[:16]}"
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    cola = sqs.create_queue(
        QueueName=f"polizas-recaudos-{uuid4().hex[:8]}", Attributes={"VisibilityTimeout": "1"}
    )["QueueUrl"]
    yield {"s3": s3, "sqs": sqs, "bucket": bucket, "cola": cola}
    for clave, valor in previas.items():
        if valor is None:
            os.environ.pop(clave, None)
        else:
            os.environ[clave] = valor


@pytest.fixture(scope="module")
def firma() -> Iterator[Firma]:
    firma = Firma()
    servidor = Servidor(Starlette(routes=[Route("/v1/firmas", firma.firmar, methods=["POST"])]))
    servidor.arrancar()
    firma.url = servidor.url
    yield firma
    servidor.apagar()


@pytest.fixture(scope="module")
def polizas(base: str, localstack: str, aws: dict[str, Any], firma: Firma) -> Iterator[Servidor]:
    ajustes = AjustesPolizas(
        SOLVENTA_DATABASE_URL=base,
        SQS_POLIZAS_RECAUDOS_URL=aws["cola"],
        AWS_REGION=REGION,
        AWS_ENDPOINT_URL=localstack,
        POLIZAS_BUCKET=aws["bucket"],
        ADAPTADOR_FIRMA_URL=firma.url,
        consumidor_espera_larga_s=1,
        emision_intervalo_s=0.1,
    )
    servidor = Servidor(_polizas(ajustes)).arrancar()
    yield servidor
    servidor.apagar()


def _portal(monkeypatch: pytest.MonkeyPatch, polizas_url: str) -> TestClient:
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    monkeypatch.setattr(settings, "polizas_url", polizas_url)
    ruta.polizas.cache_clear()
    return TestClient(portal_app, headers=ASESOR)


@pytest.fixture
def portal(
    polizas: Servidor, base: str, firma: Firma, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(f"TRUNCATE {TABLAS}")
    firma.caida = False
    with _portal(monkeypatch, polizas.url) as cliente:
        yield cliente
    ruta.polizas.cache_clear()


# --- Ayudas -----------------------------------------------------------------------------------


def _aceptar(polizas: Servidor) -> dict[str, Any]:
    """Suscripción de S01 y aceptación de la oferta, por la API de polizas."""
    with httpx.Client(base_url=polizas.url, headers=ACTOR_POLIZAS, timeout=10) as api:
        decision = api.post(
            "/v1/suscripciones",
            json={"cotizacionId": S01["cotizacionId"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert decision.status_code == 201, decision.json()
        aceptacion = api.post(
            f"/v1/cotizaciones/{S01['cotizacionId']}/aceptacion",
            json={
                "decisionId": decision.json()["id"],
                "versionDeclaraciones": "2026.10.1",
                "declaraciones": TODAS,
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert aceptacion.status_code == 201, aceptacion.json()
    resultado: dict[str, Any] = aceptacion.json()
    return resultado


def _confirmar(aws: dict[str, Any], clave: str) -> None:
    """El `RecaudoConfirmado` v1 de dinero, en la cola de polizas (como lo entrega SNS en crudo)."""
    recaudo_id = str(uuid4())
    sobre = {
        "eventId": str(uuid4()),
        "aggregateId": recaudo_id,
        "type": "RecaudoConfirmado",
        "version": 1,
        "occurredAt": RESUELTO_EN,
        "correlationId": "corr-bff-w17-modulo",
        "payload": {
            "recaudoId": recaudo_id,
            "claveRecaudo": clave,
            "cotizacionId": S01["cotizacionId"],
            "monto": {"valor": "64638.10", "moneda": "COP"},
            "transaccionId": "TX-SINTETICA-0001",
            "confirmadoEn": RESUELTO_EN,
        },
    }
    aws["sqs"].send_message(
        QueueUrl=aws["cola"],
        MessageBody=json.dumps(sobre),
        MessageAttributes={"eventType": {"DataType": "String", "StringValue": "RecaudoConfirmado"}},
    )


def _esperar(base: str, consulta: str, limite_s: float = 30) -> dict[str, Any]:
    fin = time.monotonic() + limite_s
    while True:
        with psycopg.connect(base) as conexion:
            fila = conexion.cursor(row_factory=dict_row).execute(consulta).fetchone()
        if fila is not None:
            return fila
        assert time.monotonic() < fin, f"no se cumplió a tiempo: {consulta}"
        time.sleep(0.1)


def _poliza(portal: TestClient, cotizacion_id: str) -> httpx.Response:
    return portal.get(f"/v1/cotizaciones/{cotizacion_id}/poliza")


def _documento(portal: TestClient, poliza_id: str) -> httpx.Response:
    return portal.get(f"/v1/polizas/{poliza_id}/documento")


# --- Casos ------------------------------------------------------------------------------------


def test_emitiendo_200_y_su_documento_409(
    portal: TestClient, polizas: Servidor, aws: dict[str, Any], firma: Firma, base: str
) -> None:
    # La firma caída deja la póliza EMITIENDO con el documento ya subido.
    firma.caida = True
    aceptacion = _aceptar(polizas)
    _confirmar(aws, aceptacion["id"])
    fila = _esperar(
        base,
        "SELECT poliza_id, numero, documento_huella FROM polizas"
        " WHERE documento_version_id IS NOT NULL AND ultimo_fallo = 'FIRMA_NO_DISPONIBLE'",
    )

    respuesta = _poliza(portal, aceptacion["cotizacionId"])
    assert respuesta.status_code == 200, respuesta.json()
    poliza = respuesta.json()
    assert (poliza["polizaId"], poliza["estado"], poliza["numero"]) == (
        str(fila["poliza_id"]),
        "EMITIENDO",
        fila["numero"],
    )
    assert (poliza["emitidaEn"], poliza["firma"]) == (None, None)
    assert poliza["documento"]["huella"] == fila["documento_huella"]
    assert datetime.fromisoformat(poliza["aceptadaEn"]) == datetime.fromisoformat(
        aceptacion["aceptadaEn"]
    )

    documento = _documento(portal, poliza["polizaId"])
    assert (documento.status_code, documento.json()["codigo"]) == (409, "POLIZA_NO_ACTIVA")
    assert documento.json()["detalles"] == {}


def test_activa_200_y_la_url_descarga_el_pdf(
    portal: TestClient, polizas: Servidor, aws: dict[str, Any], base: str
) -> None:
    aceptacion = _aceptar(polizas)
    _confirmar(aws, aceptacion["id"])
    fila = _esperar(
        base,
        "SELECT poliza_id, documento_version_id, documento_tamano_bytes FROM polizas"
        " WHERE estado = 'ACTIVA'",
    )

    respuesta = _poliza(portal, aceptacion["cotizacionId"])
    assert respuesta.status_code == 200, respuesta.json()
    poliza = respuesta.json()
    assert (poliza["polizaId"], poliza["estado"]) == (str(fila["poliza_id"]), "ACTIVA")
    assert poliza["documento"]["versionId"] == fila["documento_version_id"]
    assert poliza["firma"]["firmaId"] == "FIR-SINT-C01"
    assert poliza["emitidaEn"] is not None
    assert (poliza["prima"], poliza["moneda"], poliza["periodicidad"]) == (
        S01["primaTotal"],
        S01["moneda"],
        S01["periodicidad"],
    )
    assert poliza["coberturas"] == S01["coberturas"]
    assert poliza["ramo"] == {"codigo": "100", "nombre": "Vida hipotecario"}

    enlace = _documento(portal, poliza["polizaId"])
    assert enlace.status_code == 200, enlace.json()
    assert set(enlace.json()) == {"url", "venceEn"}
    url = enlace.json()["url"]
    assert parse_qs(urlsplit(url).query)["versionId"] == [fila["documento_version_id"]]
    # La URL que entrega el portal descarga la versión registrada: su sha256 es la huella.
    descarga = httpx.get(url, timeout=10)
    assert descarga.status_code == 200
    assert descarga.content.startswith(b"%PDF-")
    assert "sha256:" + hashlib.sha256(descarga.content).hexdigest() == poliza["documento"]["huella"]
    assert len(descarga.content) == fila["documento_tamano_bytes"]


def test_sin_poliza_404(portal: TestClient, polizas: Servidor) -> None:
    # Aceptada y con el recaudo pendiente: no hay póliza (AC2).
    aceptacion = _aceptar(polizas)
    respuesta = _poliza(portal, aceptacion["cotizacionId"])
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "POLIZA_NO_ENCONTRADA")
    assert respuesta.json()["detalles"] == {}
    documento = _documento(portal, str(uuid4()))
    assert (documento.status_code, documento.json()["codigo"]) == (404, "POLIZA_NO_ENCONTRADA")
    # Sin aceptación, el 404 de la aceptación.
    otra = _poliza(portal, str(uuid4()))
    assert (otra.status_code, otra.json()["codigo"]) == (404, "ACEPTACION_NO_ENCONTRADA")


def test_polizas_caido_503(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as zocalo:
        zocalo.bind(("127.0.0.1", 0))
        cerrado = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    pedidos: list[Callable[[TestClient], httpx.Response]] = [
        lambda portal: _poliza(portal, S01["cotizacionId"]),
        lambda portal: _documento(portal, str(uuid4())),
    ]
    with _portal(monkeypatch, cerrado) as portal:
        respuestas = [pedir(portal) for pedir in pedidos]
    ruta.polizas.cache_clear()
    for respuesta in respuestas:
        assert respuesta.status_code == 503
        assert (respuesta.json()["codigo"], respuesta.json()["detalles"]) == (
            "SERVICIO_NO_DISPONIBLE",
            {},
        )
