"""GET /v1/cotizaciones ("Cotizaciones abiertas") con cotizacion y clientes simulados: reenvía los
filtros como asesor, compone el nombre del cliente una vez por cliente y falla cerrado."""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import cotizaciones as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.application.cotizaciones import Cotizaciones
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
}
C01 = "00000000-0000-4000-8000-000000000001"
C02 = "00000000-0000-4000-8000-000000000002"


def item(n: int, cliente: str = C01, **cambios: Any) -> dict[str, Any]:
    return {
        "cotizacionId": f"00000000-0000-4000-d000-{n:012d}",
        "codigo": f"COT-2026-{n:05d}",
        "clienteId": cliente,
        "socioId": None,
        "productoId": "vida-hipotecario",
        "versionProducto": "2026.09.1",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "sumaAsegurada": "180000000",
        "prima": "33264",
        "nivelRiesgo": "BAJO",
        "degradada": False,
        "creadaEn": "2026-10-15T15:00:00Z",
        "vigenteHasta": "2026-10-22T15:00:00Z",
        "estado": "VIGENTE",
    } | cambios


PAGINA = {
    "items": [item(3), item(2, C02, nivelRiesgo=None, degradada=True), item(1)],
    "siguienteCursor": "c2lndWllbnRl",
    "total": 7,
}


class Simulados:
    def __init__(self) -> None:
        self.a_cotizacion: list[httpx.Request] = []
        self.a_clientes: list[httpx.Request] = []
        self.cotizacion: httpx.Response | Exception = httpx.Response(200, json=PAGINA)
        self.clientes: dict[str, httpx.Response | Exception] = {
            C01: httpx.Response(
                200, json={"clienteId": C01, "nombreCompleto": "Cliente Prueba 01"}
            ),
            C02: httpx.ConnectError("sin red"),
        }

    def responder_cotizacion(self, solicitud: httpx.Request) -> httpx.Response:
        self.a_cotizacion.append(solicitud)
        if isinstance(self.cotizacion, Exception):
            raise self.cotizacion
        return self.cotizacion

    def responder_clientes(self, solicitud: httpx.Request) -> httpx.Response:
        self.a_clientes.append(solicitud)
        respuesta = self.clientes[solicitud.url.path.rsplit("/", 1)[-1]]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


@pytest.fixture
def simulados(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulados]:
    falsos = Simulados()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cotizacion = CotizacionCliente(
        "http://cotizacion:8080", 1500, httpx.MockTransport(falsos.responder_cotizacion)
    )
    clientes = ClientesCliente(
        "http://clientes:8080", 1500, httpx.MockTransport(falsos.responder_clientes)
    )
    app.dependency_overrides[ruta.cotizaciones] = lambda: Cotizaciones(cotizacion, clientes, 500)
    yield falsos
    app.dependency_overrides.clear()


def _listar(**parametros: Any) -> httpx.Response:
    return TestClient(app).get("/v1/cotizaciones", params=parametros, headers=ASESOR)


def test_reenvia_los_filtros_como_asesor(simulados: Simulados) -> None:
    respuesta = _listar(estado="VIGENTE", q="COT-2026", limite=3, cursor="abc")
    assert respuesta.status_code == 200
    (enviada,) = simulados.a_cotizacion
    assert enviada.url.path == "/v1/cotizaciones"
    assert dict(enviada.url.params) == {
        "estado": "VIGENTE",
        "q": "COT-2026",
        "limite": "3",
        "cursor": "abc",
    }
    assert (enviada.headers["X-Actor-Id"], enviada.headers["X-Actor-Rol"]) == (
        "sub-asesora",
        "ASESOR",
    )
    cuerpo = respuesta.json()
    assert (cuerpo["total"], cuerpo["siguienteCursor"]) == (7, "c2lndWllbnRl")


def test_sin_filtros_solo_manda_el_limite(simulados: Simulados) -> None:
    _listar()
    assert dict(simulados.a_cotizacion[0].url.params) == {"limite": "20"}


def test_compone_el_nombre_una_vez_por_cliente(simulados: Simulados) -> None:
    items = _listar().json()["items"]
    assert [(i["codigo"], i["clienteNombre"]) for i in items] == [
        ("COT-2026-00003", "Cliente Prueba 01"),
        # clientes no respondió para C02: la fila sale igual, sin nombre.
        ("COT-2026-00002", None),
        ("COT-2026-00001", "Cliente Prueba 01"),
    ]
    assert sorted(s.url.path for s in simulados.a_clientes) == [
        f"/v1/clientes/{C01}",
        f"/v1/clientes/{C02}",
    ]
    assert items[1]["degradada"] is True and items[1]["nivelRiesgo"] is None


def test_sin_clientes_configurado_no_hay_nombres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cotizacion = CotizacionCliente(
        "http://cotizacion:8080",
        1500,
        httpx.MockTransport(lambda _: httpx.Response(200, json=PAGINA)),
    )
    app.dependency_overrides[ruta.cotizaciones] = lambda: Cotizaciones(cotizacion)
    try:
        items = _listar().json()["items"]
    finally:
        app.dependency_overrides.clear()
    assert {i["clienteNombre"] for i in items} == {None}


@pytest.mark.parametrize(
    ("respuesta", "estado", "codigo"),
    [
        (
            httpx.Response(422, json={"codigo": "SOLICITUD_INVALIDA", "mensaje": "cursor"}),
            422,
            "SOLICITUD_INVALIDA",
        ),
        (httpx.Response(500, text="falla"), 503, "SERVICIO_NO_DISPONIBLE"),
        (httpx.ConnectError("sin red"), 503, "SERVICIO_NO_DISPONIBLE"),
    ],
)
def test_errores_de_cotizacion(
    simulados: Simulados, respuesta: Any, estado: int, codigo: str
) -> None:
    simulados.cotizacion = respuesta
    resultado = _listar()
    assert (resultado.status_code, resultado.json()["codigo"]) == (estado, codigo)
    assert "cotizacion:8080" not in resultado.text
    assert simulados.a_clientes == []


@pytest.mark.parametrize(
    "parametros",
    [{"estado": "ACEPTADA"}, {"limite": 0}, {"limite": 101}, {"q": ""}, {"cursor": "x" * 201}],
)
def test_parametros_invalidos_no_llegan_a_cotizacion(
    simulados: Simulados, parametros: dict[str, Any]
) -> None:
    respuesta = _listar(**parametros)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert simulados.a_cotizacion == []


def test_solo_asesores(simulados: Simulados) -> None:
    otro = {**ASESOR, "X-Authenticated-Groups": "[operacion]"}
    respuesta = TestClient(app).get("/v1/cotizaciones", headers=otro)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    assert simulados.a_cotizacion == []


def test_la_dependencia_real_trae_clientes() -> None:
    ruta.cotizaciones.cache_clear()
    try:
        servicio = ruta.cotizaciones()
        assert isinstance(servicio.clientes, ClientesCliente)
    finally:
        ruta.cotizaciones.cache_clear()
