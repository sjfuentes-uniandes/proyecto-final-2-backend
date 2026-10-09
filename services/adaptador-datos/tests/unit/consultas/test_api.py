import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from adaptador_datos.config import Settings
from adaptador_datos.main import crear_app
from fastapi.testclient import TestClient

CLIENTE = "00000000-0000-4000-8000-000000000001"
CONSENTIMIENTO = "00000000-0000-4000-9000-000000000001"
CORRELACION = "corr-api-w10"
NOMINAL = {
    "active-products": {
        "schemaVersion": "3.1",
        "confidenceScore": 0.95,
        "data": {
            "oldestProductYears": 7,
            "totalMonthlyInstallment": {"amount": "1800000.00", "currency": "COP"},
            "creditorCount": 2,
        },
    },
    "payment-history": {"schemaVersion": "3.1", "confidenceScore": 0.91, "data": {"lateCount": 0}},
}

Manejador = Callable[[httpx.Request], httpx.Response]


def _nominal(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=NOMINAL[request.url.path.rsplit("/", 1)[-1]])


@pytest.fixture
def cliente_con() -> Iterator[Callable[[Manejador], tuple[TestClient, list[httpx.Request]]]]:
    abiertos: list[TestClient] = []

    def crear(manejador: Manejador) -> tuple[TestClient, list[httpx.Request]]:
        recibidas: list[httpx.Request] = []

        async def registrar(request: httpx.Request) -> httpx.Response:
            recibidas.append(request)
            respuesta = manejador(request)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        app = crear_app(Settings(), transport=httpx.MockTransport(registrar))
        cliente = TestClient(app, headers={"X-Correlation-Id": CORRELACION})
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente, recibidas

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "fuente": "FA_HISTORIAL_PAGOS_12M",
        "clienteId": CLIENTE,
        "consentimientoId": CONSENTIMIENTO,
        "proposito": "PERFILAMIENTO_PRECIO",
        "camposAutorizados": ["moras_12m"],
    }
    cuerpo.update(cambios)
    return cuerpo


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


def test_200_respuesta_canonica(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_nominal)
    respuesta = cliente.post("/v1/consultas", json=_cuerpo(), headers={"X-Deadline-Ms": "290"})
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["consultadaEn"].endswith("Z")
    cuerpo.pop("consultadaEn")
    assert cuerpo == {
        "fuente": "FA_HISTORIAL_PAGOS_12M",
        "tipo": "FINANZAS_ABIERTAS",
        "proveedor": "SIM-OPEN-FINANCE",
        "versionFuente": "3.1",
        "confianza": 0.91,
        "campos": {"moras_12m": {"estado": "DISPONIBLE", "valor": "0"}},
        "camposDescartados": 0,
    }
    (request,) = recibidas
    assert request.url.path == f"/open-finance/v3/customers/{CLIENTE}/payment-history"
    assert request.headers["X-Api-Key"] == "local-sintetico"
    assert request.headers["X-Correlation-Id"] == CORRELACION
    assert request.extensions["timeout"]["read"] == 0.27


def test_200_valores_decimales_en_texto_y_no_disponible(cliente_con: Any) -> None:
    def manejador(request: httpx.Request) -> httpx.Response:
        cuerpo = NOMINAL["active-products"] | {
            "data": {"totalMonthlyInstallment": {"amount": "1800000.00", "currency": "COP"}}
        }
        return httpx.Response(200, json=cuerpo)

    cliente, _ = cliente_con(manejador)
    respuesta = cliente.post(
        "/v1/consultas",
        json=_cuerpo(
            fuente="FA_PRODUCTOS_VIGENTES",
            camposAutorizados=["cuota_mensual_obligaciones", "entidades_con_deuda"],
        ),
    )
    assert respuesta.json()["campos"] == {
        "cuota_mensual_obligaciones": {"estado": "DISPONIBLE", "valor": "1800000.00"},
        "entidades_con_deuda": {"estado": "NO_DISPONIBLE", "valor": None},
    }


@pytest.mark.parametrize(
    ("cambios", "headers", "campos"),
    [
        ({"fuente": "FA_DESCONOCIDA"}, {}, ["fuente"]),
        ({"camposAutorizados": []}, {}, ["camposAutorizados"]),
        ({"camposAutorizados": ["moras_12m", "moras_12m"]}, {}, ["camposAutorizados"]),
        ({"proposito": "MERCADEO"}, {}, ["proposito"]),
        ({"clienteId": "no-uuid"}, {}, ["clienteId"]),
        ({}, {"X-Deadline-Ms": "701"}, ["X-Deadline-Ms"]),
        ({}, {"X-Deadline-Ms": "0"}, ["X-Deadline-Ms"]),
    ],
)
def test_422_solicitud_invalida(
    cliente_con: Any, cambios: dict[str, Any], headers: dict[str, str], campos: list[str]
) -> None:
    cliente, recibidas = cliente_con(_nominal)
    respuesta = cliente.post("/v1/consultas", json=_cuerpo(**cambios), headers=headers)
    assert _error(respuesta, 422, "SOLICITUD_INVALIDA")["detalles"] == {"campos": campos}
    assert recibidas == []


def test_422_campos_no_soportados(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_nominal)
    respuesta = cliente.post(
        "/v1/consultas", json=_cuerpo(camposAutorizados=["moras_12m", "ingreso_mensual_estimado"])
    )
    cuerpo = _error(respuesta, 422, "CAMPOS_NO_SOPORTADOS")
    assert cuerpo["detalles"] == {"campos": ["ingreso_mensual_estimado"]}
    assert recibidas == []


@pytest.mark.parametrize("cambios", [{"consentimientoId": None}, {"consentimientoId": "ausente"}])
def test_422_consentimiento_requerido_sin_llamar_al_proveedor(
    cliente_con: Any, cambios: dict[str, Any]
) -> None:
    cliente, recibidas = cliente_con(_nominal)
    cuerpo = _cuerpo(**cambios)
    if cuerpo["consentimientoId"] == "ausente":
        del cuerpo["consentimientoId"]
    _error(cliente.post("/v1/consultas", json=cuerpo), 422, "CONSENTIMIENTO_REQUERIDO")
    assert recibidas == []


def test_504_tiempo_agotado(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda r: httpx.ReadTimeout("lento", request=r))
    _error(cliente.post("/v1/consultas", json=_cuerpo()), 504, "TIEMPO_AGOTADO")


def test_503_fuente_no_disponible(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda _: httpx.Response(503, json={"error": "unavailable"}))
    cuerpo = _error(cliente.post("/v1/consultas", json=_cuerpo()), 503, "FUENTE_NO_DISPONIBLE")
    assert cuerpo["detalles"] == {"motivo": "ERROR", "circuito": "CERRADO"}


def test_503_error_de_conexion(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda r: httpx.ConnectError("caído", request=r))
    _error(cliente.post("/v1/consultas", json=_cuerpo()), 503, "FUENTE_NO_DISPONIBLE")


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(
            200, json={"schemaVersion": "3.1", "confidenceScore": 0.9, "data": "no-es-objeto"}
        ),
        httpx.Response(404, json={"error": "customer_not_found"}),
        httpx.Response(200, text="<html>"),
    ],
)
def test_502_respuesta_invalida(cliente_con: Any, respuesta: httpx.Response) -> None:
    cliente, _ = cliente_con(lambda _: respuesta)
    cuerpo = _error(cliente.post("/v1/consultas", json=_cuerpo()), 502, "RESPUESTA_INVALIDA")
    assert cuerpo["detalles"] == {}


def test_health_sin_dependencias(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_nominal)
    assert cliente.get("/health").json() == {"status": "ok"}
    assert recibidas == []


def test_el_lifespan_cierra_los_clientes() -> None:
    app = crear_app(Settings(), transport=httpx.MockTransport(_nominal))
    with TestClient(app):
        caso = app.state.consultar_fuente
    clientes = [p._cliente for p in caso._proveedores.values()]
    assert all(c.is_closed for c in clientes)
    assert asyncio.iscoroutinefunction(caso.ejecutar)
