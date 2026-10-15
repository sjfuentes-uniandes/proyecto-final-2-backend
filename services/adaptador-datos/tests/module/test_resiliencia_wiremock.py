"""Pruebas de módulo de la protección (MT-W12-01 a 04): WireMock real y una app por prueba,
para que el estado del circuito no pase de una prueba a otra."""

import time
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

CONSENTIMIENTO = "00000000-0000-4000-9000-000000000001"
INGRESOS = "FA_INGRESOS_AGREGADOS"
CAMPOS = {
    INGRESOS: ["ingreso_mensual_estimado"],
    "DA_REGISTROS_PUBLICOS": ["coincidencias_listas_restrictivas"],
}


def _cliente(nn: str) -> str:
    return f"00000000-0000-4000-8000-0000000000{nn}"


def _consultar(adaptador: Any, nn: str, fuente: str, headers: dict[str, str] | None = None) -> Any:
    return adaptador.cliente.post(
        "/v1/consultas",
        json={
            "fuente": fuente,
            "clienteId": _cliente(nn),
            "consentimientoId": CONSENTIMIENTO,
            "proposito": "PERFILAMIENTO_PRECIO",
            "camposAutorizados": CAMPOS[fuente],
        },
        headers={"X-Correlation-Id": "corr-modulo-w12", **(headers or {})},
    )


def _detalles(respuesta: Any) -> dict[str, Any]:
    assert respuesta.status_code == 503, respuesta.text
    assert respuesta.json()["codigo"] == "FUENTE_NO_DISPONIBLE"
    detalles: dict[str, Any] = respuesta.json()["detalles"]
    return detalles


def test_mt_w12_01_c07_reintenta_una_vez(app_adaptador: Any, wiremock: Any) -> None:
    respuesta = _consultar(app_adaptador(), "07", INGRESOS)
    assert _detalles(respuesta) == {"motivo": "ERROR", "circuito": "CERRADO"}
    assert len(wiremock.de_cliente(_cliente("07"))) == 2


def test_mt_w12_02_cinco_fallos_abren_el_circuito(app_adaptador: Any, wiremock: Any) -> None:
    adaptador = app_adaptador(SOLVENTA_CB_FALLOS_CONSECUTIVOS="5")
    for _ in range(5):
        _detalles(_consultar(adaptador, "07", INGRESOS))
    solicitudes = len(wiremock.de_cliente(_cliente("07")))
    assert solicitudes == 10  # 5 consultas con su reintento

    inicio = time.perf_counter()
    respuesta = _consultar(adaptador, "07", INGRESOS)
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert _detalles(respuesta) == {"motivo": "CIRCUITO_ABIERTO", "circuito": "ABIERTO"}
    assert duracion_ms < 20, duracion_ms
    assert len(wiremock.de_cliente(_cliente("07"))) == solicitudes


def test_mt_w12_03_semiabierto_y_cierre(app_adaptador: Any, wiremock: Any) -> None:
    adaptador = app_adaptador(SOLVENTA_CB_FALLOS_CONSECUTIVOS="5", SOLVENTA_CB_APERTURA_S="1")
    for _ in range(5):
        _detalles(_consultar(adaptador, "07", INGRESOS))
    assert _detalles(_consultar(adaptador, "01", INGRESOS))["circuito"] == "ABIERTO"
    assert wiremock.de_cliente(_cliente("01")) == []

    time.sleep(1.1)
    # La clave del circuito es la fuente: la prueba en SEMIABIERTO puede ser de otro cliente.
    prueba = _consultar(adaptador, "01", INGRESOS)
    assert prueba.status_code == 200, prueba.text
    assert len(wiremock.de_cliente(_cliente("01"))) == 1
    # CERRADO con el contador en 0: un fallo nuevo no lo abre.
    assert _detalles(_consultar(adaptador, "07", INGRESOS)) == {
        "motivo": "ERROR",
        "circuito": "CERRADO",
    }


def test_mt_w12_04_c06_lento_no_reintenta(app_adaptador: Any, wiremock: Any) -> None:
    adaptador = app_adaptador()
    inicio = time.perf_counter()
    respuesta = _consultar(adaptador, "06", "DA_REGISTROS_PUBLICOS", {"X-Deadline-Ms": "290"})
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert respuesta.status_code == 504, respuesta.text
    assert respuesta.json()["codigo"] == "TIEMPO_AGOTADO"
    assert duracion_ms <= 340, duracion_ms
    assert len(wiremock.de_cliente(_cliente("06"))) == 1
