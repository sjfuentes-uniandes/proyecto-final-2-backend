"""Pruebas de módulo: adaptador-pagos contra WireMock con los mapeos de desembolsos (HU-W23).

El `beneficiarioRef` sintético elige el caso (services/simulador-aliados/README.md, «Desembolsos
por beneficiario sintético»).
"""

import json
import time
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

CONFIRMA = "ben-sint-001"
SEMILLA_RECHAZA = "ben-sint-008"  # VEN-01 de la semilla de R7
RECHAZA = "ben-sint-rechaza"
LENTO = "ben-sint-lento"
CAIDO = "ben-sint-caido"
DUPLICADO = "ben-sint-duplicado"
DESCONOCIDO = "ben-sint-999"
REFERENCIA = "IND-88-2026-0442-01"
CORRELACION = "corr-modulo-w23"


def _ordenar(adaptador: Any, beneficiario: str, referencia: str = REFERENCIA) -> Any:
    return adaptador.cliente.post(
        "/v1/desembolsos",
        json={
            "referencia": referencia,
            "monto": "2500000.00",
            "moneda": "COP",
            "beneficiarioRef": beneficiario,
        },
        headers={"X-Correlation-Id": CORRELACION},
    )


def _consultar(adaptador: Any, beneficiario: str, referencia: str = REFERENCIA) -> Any:
    return adaptador.cliente.get(
        f"/v1/desembolsos/{referencia}",
        params={"beneficiarioRef": beneficiario},
        headers={"X-Correlation-Id": CORRELACION},
    )


def _error(respuesta: Any, estado: int, codigo: str) -> None:
    assert respuesta.status_code == estado, respuesta.text
    assert respuesta.json()["codigo"] == codigo
    assert respuesta.json()["correlationId"] == CORRELACION


def _confirmado(referencia: str = REFERENCIA, transaccion: str | None = None) -> dict[str, Any]:
    return {
        "referencia": referencia,
        "estado": "CONFIRMADO",
        "transaccionId": transaccion or f"TRX-SINT-{referencia}",
        "motivo": None,
    }


RECHAZADO = {
    "referencia": REFERENCIA,
    "estado": "RECHAZADO",
    "transaccionId": None,
    "motivo": "BENEFICIARIO_NO_HABILITADO",
}


# --- Confirmado -----------------------------------------------------------------------------
def test_desembolso_confirmado(app_adaptador: Any) -> None:
    respuesta = _ordenar(app_adaptador(), CONFIRMA)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == _confirmado()


def test_consulta_confirmado(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), CONFIRMA)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == _confirmado()


# --- Rechazado: la semilla (ben-sint-008) y el beneficiario de pruebas -----------------------
@pytest.mark.parametrize("beneficiario", [SEMILLA_RECHAZA, RECHAZA])
def test_desembolso_rechazado_con_motivo(app_adaptador: Any, beneficiario: str) -> None:
    respuesta = _ordenar(app_adaptador(), beneficiario)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == RECHAZADO


def test_consulta_rechazado(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), RECHAZA)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == RECHAZADO


# --- Lento: el adaptador corta; la consulta dice que el desembolso sí ocurrió -----------------
def test_desembolso_lento_agota_el_timeout(app_adaptador: Any) -> None:
    """El simulador tarda 8000 ms; con un timeout de prueba de 1 s el adaptador responde 504."""
    adaptador = app_adaptador(ALLY_PAGOS_TIMEOUT_MS="1000")
    inicio = time.perf_counter()
    respuesta = _ordenar(adaptador, LENTO)
    duracion_ms = (time.perf_counter() - inicio) * 1000
    _error(respuesta, 504, "TIEMPO_AGOTADO")
    assert 1000 <= duracion_ms < 3000, duracion_ms


def test_consulta_del_lento_confirma(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), LENTO)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == _confirmado()


# --- Caído: la orden nunca llegó ------------------------------------------------------------
def test_desembolso_caido_es_503(app_adaptador: Any) -> None:
    _error(_ordenar(app_adaptador(), CAIDO), 503, "FUENTE_NO_DISPONIBLE")


def test_consulta_desembolso_no_encontrado(app_adaptador: Any) -> None:
    _error(_consultar(app_adaptador(), CAIDO), 404, "DESEMBOLSO_NO_ENCONTRADO")


# --- Duplicado: la misma referencia devuelve el mismo transaccionId ---------------------------
def test_duplicado_devuelve_el_mismo_transaccion_id(app_adaptador: Any) -> None:
    adaptador = app_adaptador()
    esperado = _confirmado(transaccion="TRX-SINT-DUPLICADO")
    assert [_ordenar(adaptador, DUPLICADO).json() for _ in range(3)] == [esperado] * 3
    assert _consultar(adaptador, DUPLICADO).json() == esperado


# --- Beneficiario desconocido: el respaldo responde referencia_no_reconocida ------------------
def test_desconocido_desembolso_es_502(app_adaptador: Any) -> None:
    _error(_ordenar(app_adaptador(), DESCONOCIDO), 502, "RESPUESTA_INVALIDA")


def test_desconocido_consulta_es_502(app_adaptador: Any) -> None:
    _error(_consultar(app_adaptador(), DESCONOCIDO), 502, "RESPUESTA_INVALIDA")


# --- Lo que recibe el proveedor y lo que queda en los logs ----------------------------------
def test_journal_del_desembolso(app_adaptador: Any, wiremock: Any) -> None:
    assert _ordenar(app_adaptador(), CONFIRMA).status_code == 200
    (solicitud,) = wiremock.solicitudes()
    cabeceras = {clave.lower(): valor for clave, valor in solicitud["headers"].items()}
    assert cabeceras["idempotency-key"] == REFERENCIA
    assert cabeceras["x-api-key"] == "local-sintetico"
    assert cabeceras["x-correlation-id"] == CORRELACION
    assert json.loads(solicitud["body"]) == {
        "referencia": REFERENCIA,
        "monto": "2500000.00",
        "moneda": "COP",
        "beneficiario": CONFIRMA,
    }


def test_journal_de_la_consulta(app_adaptador: Any, wiremock: Any) -> None:
    assert _consultar(app_adaptador(), CONFIRMA).status_code == 200
    (solicitud,) = wiremock.solicitudes()
    assert solicitud["method"] == "GET"
    assert solicitud["queryParams"]["referencia"]["values"] == [REFERENCIA]
    assert solicitud["queryParams"]["beneficiario"]["values"] == [CONFIRMA]


@pytest.mark.parametrize("beneficiario", [CONFIRMA, CAIDO])
def test_logs_sin_beneficiario_ni_cuerpos(app_adaptador: Any, beneficiario: str) -> None:
    adaptador = app_adaptador()
    _ordenar(adaptador, beneficiario)
    _consultar(adaptador, beneficiario)
    texto = adaptador.logs.getvalue()
    assert '"operation": "desembolso.ordenar"' in texto
    assert '"operation": "desembolso.consultar"' in texto
    for centinela in (beneficiario, "local-sintetico", "2500000.00", "TRX-SINT"):
        assert centinela not in texto, centinela
