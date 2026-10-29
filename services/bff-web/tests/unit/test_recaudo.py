"""UT-W16 (bff-web, 16.14): estado del recaudo en el portal (HU-W16) con polizas simulado.

Solo `asesores` consulta (los demás grupos, 403 sin llamar a polizas); actor, rol y correlación
hacia polizas; los 4xx de la lista blanca pasan tal cual y todo lo demás es 503
SERVICIO_NO_DISPONIBLE sin detalles (falla cerrada).
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import recaudos as ruta
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.recaudo import Recaudos
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
COTIZACION_ID = "00000000-0000-4000-a000-000000000001"
RUTA = f"/v1/cotizaciones/{COTIZACION_ID}/recaudo"
TRANSACCION = "TX-SINTETICA-0001"
PENDIENTE: dict[str, Any] = {
    "estado": "PENDIENTE",
    "claveRecaudo": "00000000-0000-4000-f000-000000000001",
    "monto": "64638",
    "moneda": "COP",
    "transaccionId": None,
    "motivo": None,
    "actualizadoEn": "2026-10-24T15:00:00Z",
}
CONFIRMADO = {
    **PENDIENTE,
    "estado": "CONFIRMADO",
    "monto": "64638.10",
    "transaccionId": TRANSACCION,
    "actualizadoEn": "2026-10-24T15:00:01.250000Z",
}
RECHAZADO = {
    **PENDIENTE,
    "estado": "RECHAZADO",
    "motivo": "FONDOS_INSUFICIENTES",
    "actualizadoEn": "2026-10-24T15:00:01.250000Z",
}


def _usuario(grupos: str, sub: str = "sub-asesor") -> dict[str, str]:
    return {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Authenticated-Sub": sub,
        "X-Correlation-Id": "corr-bff-w16",
    }


ASESOR = _usuario("[asesores]")
SUSCRIPTOR = _usuario("[suscriptores]", "sub-suscriptora")
OPERACION = _usuario("[operacion]")


class Simulado:
    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(
            200, json=PENDIENTE
        )

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)


@pytest.fixture
def polizas(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulado]:
    simulado = Simulado()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = PolizasCliente("http://polizas:8080", 1500, httpx.MockTransport(simulado))
    app.dependency_overrides[ruta.recaudos] = lambda: Recaudos(cliente)
    yield simulado
    app.dependency_overrides.clear()


def _consultar(encabezados: dict[str, str], ruta_: str = RUTA) -> httpx.Response:
    return TestClient(app).get(ruta_, headers=encabezados)


@pytest.mark.parametrize("estado", [PENDIENTE, CONFIRMADO, RECHAZADO], ids=lambda e: e["estado"])
def test_el_asesor_consulta_el_estado_del_recaudo(
    polizas: Simulado, estado: dict[str, Any]
) -> None:
    polizas.responder = lambda _: httpx.Response(200, json=estado)
    respuesta = _consultar(ASESOR)
    assert (respuesta.status_code, respuesta.json()) == (200, estado)


def test_reenvia_a_polizas_con_el_actor_el_rol_y_la_correlacion(polizas: Simulado) -> None:
    _consultar(ASESOR)
    [enviado] = polizas.pedidos
    assert (str(enviado.url), enviado.method) == (f"http://polizas:8080{RUTA}", "GET")
    assert enviado.headers["X-Actor-Id"] == "sub-asesor"
    assert enviado.headers["X-Actor-Rol"] == "ASESOR"
    assert enviado.headers["X-Correlation-Id"] == "corr-bff-w16"
    assert enviado.content == b""


@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        (SUSCRIPTOR, 403, "ROL_NO_AUTORIZADO"),
        (OPERACION, 403, "ROL_NO_AUTORIZADO"),
        ({**ASESOR, "X-Authenticated-Issuer": "https://otro"}, 403, "ROL_NO_AUTORIZADO"),
        ({k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}, 401, "ACTOR_REQUERIDO"),
        ({**ASESOR, "X-Authenticated-Sub": ""}, 401, "ACTOR_REQUERIDO"),
    ],
    ids=["suscriptores", "operacion", "otro-emisor", "sin-sub", "sub-vacio"],
)
def test_solo_asesores_identificados(
    polizas: Simulado, encabezados: dict[str, str], estado: int, codigo: str
) -> None:
    respuesta = _consultar(encabezados)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert polizas.pedidos == []


def test_cotizacion_que_no_es_uuid_no_llama_a_polizas(polizas: Simulado) -> None:
    respuesta = _consultar(ASESOR, "/v1/cotizaciones/no-es-uuid/recaudo")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert polizas.pedidos == []


@pytest.mark.parametrize(
    ("estado", "codigo", "detalles"),
    [
        (401, "ACTOR_REQUERIDO", {}),
        (403, "ROL_NO_AUTORIZADO", {}),
        (422, "SOLICITUD_INVALIDA", {"campos": ["cotizacionId"]}),
        (404, "ACEPTACION_NO_ENCONTRADA", {}),
    ],
)
def test_los_errores_de_la_lista_blanca_pasan_tal_cual(
    polizas: Simulado, estado: int, codigo: str, detalles: dict[str, Any]
) -> None:
    polizas.responder = lambda _: httpx.Response(
        estado, json={"codigo": codigo, "mensaje": "mensaje de polizas", "detalles": detalles}
    )
    respuesta = _consultar(ASESOR)
    assert respuesta.status_code == estado
    assert respuesta.json() == {
        "codigo": codigo,
        "mensaje": "mensaje de polizas",
        "correlationId": "corr-bff-w16",
        "detalles": detalles,
    }


def _caido(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("polizas caído")


def _lento(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("polizas no respondió a tiempo")


@pytest.mark.parametrize(
    "responder",
    [
        lambda _: httpx.Response(
            503,
            json={
                "codigo": "DEPENDENCIA_NO_DISPONIBLE",
                "mensaje": "m",
                "detalles": {"dependencia": "polizas.configuracion"},
            },
        ),
        lambda _: httpx.Response(500, json={"codigo": "ERROR_INTERNO", "detalles": {"x": 1}}),
        lambda _: httpx.Response(502, json={"codigo": "ACEPTACION_NO_ENCONTRADA"}),
        lambda _: httpx.Response(409, json={"codigo": "OFERTA_VENCIDA", "detalles": {"a": 1}}),
        lambda _: httpx.Response(404, json={"codigo": "DECISION_NO_ENCONTRADA"}),
        lambda _: httpx.Response(404, text="<html>no encontrado</html>"),
        lambda _: httpx.Response(200, text="<html>"),
        lambda _: httpx.Response(200, json=[PENDIENTE]),
        lambda _: httpx.Response(200, json={**PENDIENTE, "estado": "ANULADO"}),
        lambda _: httpx.Response(200, json={**PENDIENTE, "monto": 64638.1}),
        lambda _: httpx.Response(200, json={**PENDIENTE, "moneda": "cop"}),
        lambda _: httpx.Response(200, json={k: v for k, v in PENDIENTE.items() if k != "motivo"}),
        _lento,
        _caido,
    ],
    ids=[
        "503-dependencia",
        "500",
        "5xx-con-codigo-de-la-lista",
        "4xx-fuera-de-la-lista",
        "404-de-otro-codigo",
        "404-no-json",
        "no-json",
        "no-objeto",
        "estado-desconocido",
        "monto-float",
        "moneda-invalida",
        "falta-un-campo",
        "timeout",
        "caido",
    ],
)
def test_todo_lo_demas_es_503_sin_detalles(
    polizas: Simulado, responder: Callable[..., httpx.Response]
) -> None:
    polizas.responder = responder
    respuesta = _consultar(ASESOR)
    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert (cuerpo["codigo"], cuerpo["detalles"], cuerpo["correlationId"]) == (
        "SERVICIO_NO_DISPONIBLE",
        {},
        "corr-bff-w16",
    )


def test_los_logs_no_tienen_datos_del_recaudo(
    polizas: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    polizas.responder = lambda _: httpx.Response(200, json=CONFIRMADO)
    with caplog.at_level(logging.DEBUG):
        assert _consultar(ASESOR).status_code == 200
        polizas.responder = lambda _: httpx.Response(500, json=CONFIRMADO)
        assert _consultar(ASESOR).status_code == 503
    texto = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for centinela in (TRANSACCION, "64638.10", "sub-asesor"):
        assert centinela not in texto, centinela


def test_el_servicio_por_omision_usa_la_configuracion() -> None:
    ruta.recaudos.cache_clear()
    servicio = ruta.recaudos()
    assert servicio.polizas.url == settings.polizas_url.rstrip("/")
    assert servicio.polizas.timeout == settings.polizas_timeout_ms / 1000
    ruta.recaudos.cache_clear()
