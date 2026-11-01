"""MT-W16-14 (16.14): del portal al estado del recaudo, con `polizas` real.

Cadena: `bff-web` -> `polizas` (uvicorn + PostgreSQL de Testcontainers, con sus migraciones). Solo
se simulan las dependencias de polizas que no son el objeto de la prueba (cotizacion y catalogo,
con los insumos sintéticos y la semilla de reglas 2026.10.1) y su reloj, fijado en el
`ahoraDeReferencia` para que la oferta de S01 esté vigente.

La suscripción y la aceptación se registran por la API de polizas. Sin evento de dinero, el recaudo
está PENDIENTE; con una fila de proyección insertada (lo que deja el consumidor de
`polizas-recaudos`), CONFIRMADO o RECHAZADO. Sin aceptación, 404; con polizas caído, 503. Solo
datos sintéticos.
"""

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from bff_web.adapters.inbound import recaudos as ruta
from bff_web.config import settings
from bff_web.main import app as portal_app
from fastapi.testclient import TestClient
from polizas.adapters.outbound.aceptaciones_pg import AceptacionesPg
from polizas.adapters.outbound.catalogo_http import CatalogoHttp
from polizas.adapters.outbound.codigos import CodigosPg
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.adapters.outbound.decisiones_pg import DecisionesPg, migrar
from polizas.adapters.outbound.recaudos_pg import RecaudosPg
from polizas.config import Settings as AjustesPolizas
from polizas.domain.suscripcion.insumos import huella
from polizas.main import Puertos
from polizas.main import crear_app as crear_polizas
from starlette.applications import Starlette
from starlette.routing import Mount
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
RESUELTO_EN = "2026-10-24T15:00:01.250000Z"


class Servidor:
    """Una app ASGI servida por uvicorn en un hilo propio (como la R8).

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


def _proveedores() -> httpx.MockTransport:
    """cotizacion y catalogo de polizas, como su arnés de módulo."""
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


def _polizas(url: str) -> Starlette:
    """polizas con sus adaptadores de PostgreSQL de producción y el reloj fijo. El pool y la app
    nacen en el bucle de uvicorn (psycopg_pool se ata al bucle que lo abre)."""
    estado: dict[str, Any] = {}

    @asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        from psycopg_pool import AsyncConnectionPool

        reloj = RelojFijo()
        ajustes = AjustesPolizas(SOLVENTA_DATABASE_URL=url)
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
            )
            app = crear_polizas(ajustes, puertos=puertos, pool=pool)
            async with app.router.lifespan_context(app):
                estado["app"] = app
                yield

    async def reenviar(scope: Scope, receive: Receive, send: Send) -> None:
        await estado["app"](scope, receive, send)

    return Starlette(lifespan=ciclo, routes=[Mount("/", app=reenviar)])


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    from testcontainers.community.postgres import PostgresContainer

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        pytest.skip("Docker no está disponible para testcontainers")
    with PostgresContainer("postgres:16", driver=None) as contenedor:
        url = contenedor.get_connection_url()
        migrar(url)
        yield url


@pytest.fixture(scope="module")
def polizas(base: str) -> Iterator[Servidor]:
    servidor = Servidor(_polizas(base)).arrancar()
    yield servidor
    servidor.apagar()


def _portal(monkeypatch: pytest.MonkeyPatch, polizas_url: str) -> TestClient:
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    monkeypatch.setattr(settings, "polizas_url", polizas_url)
    ruta.recaudos.cache_clear()
    return TestClient(portal_app, headers=ASESOR)


@pytest.fixture
def portal(polizas: Servidor, base: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(
            "TRUNCATE polizas, aceptaciones, decisiones_suscripcion, outbox, inbox, recaudos_aceptacion"
        )
    with _portal(monkeypatch, polizas.url) as cliente:
        yield cliente
    ruta.recaudos.cache_clear()


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


def _proyectar(base: str, clave: str, estado: str) -> None:
    """La fila que deja el consumidor de polizas-recaudos al recibir el evento de dinero."""
    valores: tuple[str | None, ...]
    if estado == "CONFIRMADO":
        valores = ("64638.10", "COP", "TX-SINTETICA-0001", None)
    else:
        valores = (None, None, None, "FONDOS_INSUFICIENTES")
    with psycopg.connect(base, autocommit=True) as conexion:
        conexion.execute(
            "INSERT INTO recaudos_aceptacion (clave_recaudo, recaudo_id, estado, monto, moneda,"
            " transaccion_id, motivo, ocurrido_en) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (clave, uuid4(), estado, *valores, RESUELTO_EN),
        )


def _recaudo(portal: TestClient, cotizacion_id: str) -> httpx.Response:
    return portal.get(f"/v1/cotizaciones/{cotizacion_id}/recaudo")


def test_sin_evento_el_recaudo_esta_pendiente(portal: TestClient, polizas: Servidor) -> None:
    aceptacion = _aceptar(polizas)
    respuesta = _recaudo(portal, aceptacion["cotizacionId"])
    assert (respuesta.status_code, respuesta.json()) == (
        200,
        {
            "estado": "PENDIENTE",
            "claveRecaudo": aceptacion["id"],
            "monto": S01["primaTotal"],
            "moneda": S01["moneda"],
            "transaccionId": None,
            "motivo": None,
            "actualizadoEn": aceptacion["aceptadaEn"],
        },
    )


def test_con_la_proyeccion_confirmada(portal: TestClient, polizas: Servidor, base: str) -> None:
    aceptacion = _aceptar(polizas)
    _proyectar(base, aceptacion["id"], "CONFIRMADO")
    respuesta = _recaudo(portal, aceptacion["cotizacionId"])
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["estado"], cuerpo["claveRecaudo"]) == ("CONFIRMADO", aceptacion["id"])
    assert (cuerpo["monto"], cuerpo["moneda"]) == ("64638.10", "COP")
    assert (cuerpo["transaccionId"], cuerpo["motivo"]) == ("TX-SINTETICA-0001", None)
    assert datetime.fromisoformat(cuerpo["actualizadoEn"]) == datetime.fromisoformat(RESUELTO_EN)


def test_con_la_proyeccion_rechazada(portal: TestClient, polizas: Servidor, base: str) -> None:
    aceptacion = _aceptar(polizas)
    _proyectar(base, aceptacion["id"], "RECHAZADO")
    respuesta = _recaudo(portal, aceptacion["cotizacionId"])
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert (cuerpo["estado"], cuerpo["claveRecaudo"]) == ("RECHAZADO", aceptacion["id"])
    # RecaudoRechazado v1 no trae monto: es la prima de la instantánea de la decisión.
    assert (cuerpo["monto"], cuerpo["moneda"]) == (S01["primaTotal"], S01["moneda"])
    assert (cuerpo["transaccionId"], cuerpo["motivo"]) == (None, "FONDOS_INSUFICIENTES")
    assert datetime.fromisoformat(cuerpo["actualizadoEn"]) == datetime.fromisoformat(RESUELTO_EN)


def test_sin_aceptacion_404(portal: TestClient) -> None:
    respuesta = _recaudo(portal, S01["cotizacionId"])
    assert respuesta.status_code == 404
    assert (respuesta.json()["codigo"], respuesta.json()["detalles"]) == (
        "ACEPTACION_NO_ENCONTRADA",
        {},
    )


def test_polizas_caido_503(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as zocalo:
        zocalo.bind(("127.0.0.1", 0))
        cerrado = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    with _portal(monkeypatch, cerrado) as portal:
        respuesta = _recaudo(portal, S01["cotizacionId"])
    ruta.recaudos.cache_clear()
    assert respuesta.status_code == 503
    assert (respuesta.json()["codigo"], respuesta.json()["detalles"]) == (
        "SERVICIO_NO_DISPONIBLE",
        {},
    )
