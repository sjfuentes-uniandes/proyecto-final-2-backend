"""MT-W18 (bff-web): del portal al estado y el listado de pólizas, con `consulta` real.

Cadena: `bff-web` (cliente HTTP real, corte de 700 ms) -> `consulta` (su app real servida con uvicorn,
con la proyección en memoria). Sin Docker. Cubre el 200 con `estado` y `fechaActualizacion`, el 404
uniforme, el listado con filtros y paginación, el 403 de otro grupo y el 503 con `consulta` caído,
lento (supera 700 ms) o respondiendo 401/403. Solo datos sintéticos.
"""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from bff_web.adapters.inbound import estado_polizas as ruta
from bff_web.adapters.outbound.consulta import ConsultaCliente
from bff_web.application.estado_poliza import EstadoPolizas
from bff_web.config import settings
from bff_web.main import app as portal_app
from consulta.application.estado.casos import BuscarPolizas, ConsultarEstadoPoliza
from consulta.application.estado.puertos import FiltroPolizas, ResultadoBusqueda
from consulta.domain.polizas.estado import estado_de
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia
from consulta.main import crear_app
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
}
SUSCRIPTOR = {**ASESOR, "X-Authenticated-Groups": "[suscriptores]"}
AHORA = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
HOY = date(2026, 10, 25)


class RelojFijo:
    def ahora(self) -> datetime:
        return AHORA


def _poliza(numero: str, desde: date, hasta: date, ramo: str, minutos: int) -> PolizaProyectada:
    return PolizaProyectada(
        poliza_id=uuid4(),
        cliente_id=uuid4(),
        numero=numero,
        estado="ACTIVA",
        producto_id="vida-deudor",
        producto_version="1.0.0",
        producto_nombre="Vida deudor",
        ramo=ramo,
        vigencia=Vigencia(desde, hasta),
        prima=Decimal("64638.10"),
        suma_asegurada=Decimal(50000000),
        moneda="COP",
        periodicidad="MENSUAL",
        coberturas=(CoberturaProyectada("MUERTE", "Muerte", True),),
        documento_huella="sha256:" + "a" * 64,
        firma_id="FIRMA-0001",
        emitida_en=AHORA - timedelta(minutes=minutos),
        proyectada_en=AHORA + timedelta(seconds=minutos),
    )


POLIZAS = (
    _poliza("100-2026-000001", date(2026, 1, 1), date(2027, 6, 30), "Vida", 1),
    _poliza("100-2026-000002", date(2025, 11, 5), date(2026, 11, 4), "Vida", 2),
    _poliza("100-2026-000003", date(2025, 1, 1), date(2025, 12, 31), "Hogar", 3),
    _poliza("100-2026-000004", date(2026, 11, 1), date(2027, 10, 31), "Hogar", 4),
)
ESTADOS = {
    "100-2026-000001": "VIGENTE",
    "100-2026-000002": "POR_VENCER",
    "100-2026-000003": "VENCIDA",
    "100-2026-000004": "NO_INICIADA",
}


class LecturaEnMemoria:
    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        return next((p for p in POLIZAS if p.poliza_id == poliza_id), None)

    async def buscar(self, filtro: FiltroPolizas, pagina: int, tamano: int) -> ResultadoBusqueda:
        coincidentes = [
            p
            for p in POLIZAS
            if (filtro.numero is None or p.numero == filtro.numero)
            and (filtro.ramo is None or p.ramo == filtro.ramo)
            and (filtro.estado is None or estado_de(p.vigencia, filtro.hoy, 30) is filtro.estado)
            and (filtro.vigencia_desde is None or p.vigencia.hasta >= filtro.vigencia_desde)
        ]
        coincidentes.sort(key=lambda p: (-p.emitida_en.timestamp(), str(p.poliza_id)))
        inicio = (pagina - 1) * tamano
        return ResultadoBusqueda(
            tuple(coincidentes[inicio : inicio + tamano]),
            len(coincidentes),
            max(p.proyectada_en for p in POLIZAS),
        )


@contextmanager
def _servir(app: Any) -> Iterator[str]:
    zocalo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zocalo.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{zocalo.getsockname()[1]}"
    servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def correr() -> None:
        asyncio.run(servidor.serve(sockets=[zocalo]), loop_factory=asyncio.SelectorEventLoop)

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    limite = time.monotonic() + 30
    while not servidor.started:
        assert hilo.is_alive(), "el servidor no arrancó"
        assert time.monotonic() < limite, "el servidor no arrancó a tiempo"
        time.sleep(0.05)
    try:
        yield url
    finally:
        servidor.should_exit = True
        hilo.join(timeout=15)


def _consulta_real() -> Any:
    app = crear_app()
    lectura = LecturaEnMemoria()
    app.state.consultar_estado_poliza = ConsultarEstadoPoliza(lectura, RelojFijo(), 30)
    app.state.buscar_polizas = BuscarPolizas(lectura, RelojFijo(), 30)
    return app


@contextmanager
def _portal(url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    servicio = EstadoPolizas(ConsultaCliente(url, settings.consulta_timeout_ms))
    portal_app.dependency_overrides[ruta.estados] = lambda: servicio
    try:
        yield TestClient(portal_app)
    finally:
        portal_app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def url_consulta() -> Iterator[str]:
    with _servir(_consulta_real()) as url:
        # Precalentamiento: el primer request paga las importaciones diferidas y no debe caer
        # dentro del corte de 700 ms de las pruebas.
        for destino in ("/v1/polizas", f"/v1/polizas/{uuid4()}/estado"):
            for _ in range(3):
                httpx.get(
                    f"{url}{destino}",
                    headers={"X-Actor-Id": "calentamiento", "X-Actor-Rol": "ASESOR"},
                    timeout=10,
                )
        yield url


@pytest.fixture
def portal(url_consulta: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _portal(url_consulta, monkeypatch) as cliente:
        yield cliente


def _id(numero: str) -> UUID:
    return next(p.poliza_id for p in POLIZAS if p.numero == numero)


@pytest.mark.parametrize(("numero", "estado"), list(ESTADOS.items()))
def test_el_asesor_recibe_el_estado_y_la_fecha_de_actualizacion(
    portal: TestClient, numero: str, estado: str
) -> None:
    poliza = next(p for p in POLIZAS if p.numero == numero)
    respuesta = portal.get(f"/v1/polizas/{poliza.poliza_id}/estado", headers=ASESOR)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == estado
    assert datetime.fromisoformat(cuerpo["fechaActualizacion"]) == poliza.proyectada_en
    assert (cuerpo["numero"], cuerpo["producto"], cuerpo["prima"]) == (
        numero,
        "Vida deudor",
        "64638.10",
    )
    assert "clienteId" not in cuerpo


def test_la_poliza_inexistente_es_404_con_correlacion(portal: TestClient) -> None:
    respuesta = portal.get(f"/v1/polizas/{uuid4()}/estado", headers=ASESOR)
    assert respuesta.status_code == 404
    cuerpo = respuesta.json()
    assert (cuerpo["codigo"], cuerpo["mensaje"]) == ("POLIZA_NO_ENCONTRADA", "La póliza no existe")
    assert cuerpo["correlationId"]


def test_el_listado_filtra_pagina_y_ordena(portal: TestClient) -> None:
    todo = portal.get("/v1/polizas", headers=ASESOR).json()
    assert [i["numero"] for i in todo["items"]] == [
        "100-2026-000001",
        "100-2026-000002",
        "100-2026-000003",
        "100-2026-000004",
    ]
    assert todo["total"] == 4
    assert datetime.fromisoformat(todo["actualizadoEn"]) == AHORA + timedelta(seconds=4)
    assert all("clienteId" not in i for i in todo["items"])

    hogar = portal.get("/v1/polizas", params={"ramo": "Hogar"}, headers=ASESOR).json()
    assert [i["numero"] for i in hogar["items"]] == ["100-2026-000003", "100-2026-000004"]
    por_vencer = portal.get("/v1/polizas", params={"estado": "POR_VENCER"}, headers=ASESOR).json()
    assert [i["numero"] for i in por_vencer["items"]] == ["100-2026-000002"]
    exacto = portal.get("/v1/polizas", params={"numero": "100-2026-000004"}, headers=ASESOR).json()
    assert [i["estado"] for i in exacto["items"]] == ["NO_INICIADA"]
    desde = portal.get("/v1/polizas", params={"vigenciaDesde": "2026-01-01"}, headers=ASESOR).json()
    assert desde["total"] == 3
    segunda = portal.get("/v1/polizas", params={"pagina": 2, "tamano": 3}, headers=ASESOR).json()
    assert (segunda["pagina"], segunda["tamano"], segunda["total"]) == (2, 3, 4)
    assert [i["numero"] for i in segunda["items"]] == ["100-2026-000004"]


def test_otro_grupo_es_403_y_un_filtro_invalido_es_422(portal: TestClient) -> None:
    assert portal.get("/v1/polizas", headers=SUSCRIPTOR).status_code == 403
    invalida = portal.get("/v1/polizas", params={"estado": "ACTIVA"}, headers=ASESOR)
    assert (invalida.status_code, invalida.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_un_parametro_ajeno_no_cambia_el_resultado(portal: TestClient) -> None:
    base = portal.get("/v1/polizas", headers=ASESOR).json()
    con_ajenos = portal.get(
        "/v1/polizas", params={"clienteId": str(uuid4()), "rol": "CLIENTE"}, headers=ASESOR
    ).json()
    assert con_ajenos == base


def test_con_consulta_caido_es_503(monkeypatch: pytest.MonkeyPatch) -> None:
    zocalo = socket.socket()
    zocalo.bind(("127.0.0.1", 0))
    puerto = zocalo.getsockname()[1]
    zocalo.close()
    with _portal(f"http://127.0.0.1:{puerto}", monkeypatch) as portal:
        for destino in ("/v1/polizas", f"/v1/polizas/{uuid4()}/estado"):
            respuesta = portal.get(destino, headers=ASESOR)
            assert (respuesta.status_code, respuesta.json()["codigo"]) == (
                503,
                "SERVICIO_NO_DISPONIBLE",
            )


async def _lenta(_: Request) -> JSONResponse:
    await asyncio.sleep(settings.consulta_timeout_ms / 1000 + 0.5)
    return JSONResponse({})


def _rechaza(estado: int) -> Any:
    async def responder(_: Request) -> JSONResponse:
        return JSONResponse({"codigo": "ROL_NO_AUTORIZADO", "mensaje": "x"}, status_code=estado)

    return responder


def test_con_consulta_lento_corta_a_los_700_ms_con_503(monkeypatch: pytest.MonkeyPatch) -> None:
    rutas = [Route("/v1/polizas", _lenta), Route("/v1/polizas/{id}/estado", _lenta)]
    with _servir(Starlette(routes=rutas)) as url, _portal(url, monkeypatch) as portal:
        inicio = time.monotonic()
        respuesta = portal.get(f"/v1/polizas/{uuid4()}/estado", headers=ASESOR)
        transcurrido = time.monotonic() - inicio
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")
    assert transcurrido < 1.2


@pytest.mark.parametrize("estado", [401, 403])
def test_si_consulta_rechaza_al_bff_es_503(monkeypatch: pytest.MonkeyPatch, estado: int) -> None:
    rutas = [
        Route("/v1/polizas", _rechaza(estado)),
        Route("/v1/polizas/{id}/estado", _rechaza(estado)),
    ]
    with _servir(Starlette(routes=rutas)) as url, _portal(url, monkeypatch) as portal:
        for destino in ("/v1/polizas", f"/v1/polizas/{uuid4()}/estado"):
            respuesta = portal.get(destino, headers=ASESOR)
            assert (respuesta.status_code, respuesta.json()["codigo"]) == (
                503,
                "SERVICIO_NO_DISPONIBLE",
            )
            assert "ROL_NO_AUTORIZADO" not in respuesta.text
