"""Pruebas de módulo: adaptador-pagos contra WireMock con los mapeos de pagos (HU-W16, 16.7).

El pagador sintético elige el caso (services/simulador-aliados/README.md, «Pagos por pagador
sintético»).
"""

import json
import time
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

C01 = "00000000-0000-4000-8000-000000000001"  # aprobado
C02 = "00000000-0000-4000-8000-000000000002"  # rechazado
C05 = "00000000-0000-4000-8000-000000000005"  # malformado
C06 = "00000000-0000-4000-8000-000000000006"  # lento (8000 ms), el cobro sí ocurre
C07 = "00000000-0000-4000-8000-000000000007"  # caído, el cobro nunca llega
DESCONOCIDO = "00000000-0000-4000-8000-0000000000ff"
CORRELACION = "corr-modulo-w16"


def _cobrar(adaptador: Any, pagador: str, clave: str | None = None) -> Any:
    return adaptador.cliente.post(
        "/v1/cobros",
        json={
            "claveRecaudo": clave or str(uuid4()),
            "monto": "64638.00",
            "moneda": "COP",
            "pagadorRef": pagador,
        },
        headers={"X-Correlation-Id": CORRELACION},
    )


def _consultar(adaptador: Any, pagador: str, clave: str | None = None) -> Any:
    return adaptador.cliente.get(
        f"/v1/cobros/{clave or uuid4()}",
        params={"pagadorRef": pagador},
        headers={"X-Correlation-Id": CORRELACION},
    )


def _error(respuesta: Any, estado: int, codigo: str) -> None:
    assert respuesta.status_code == estado, respuesta.text
    assert respuesta.json()["codigo"] == codigo
    assert respuesta.json()["correlationId"] == CORRELACION


# --- C01 aprobado ---------------------------------------------------------------------------
def test_c01_cobro_aprobado(app_adaptador: Any) -> None:
    clave = str(uuid4())
    respuesta = _cobrar(app_adaptador(), C01, clave)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "claveRecaudo": clave,
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-C01",
        "motivo": None,
    }


def test_c01_consulta_aprobado(app_adaptador: Any) -> None:
    clave = str(uuid4())
    respuesta = _consultar(app_adaptador(), C01, clave)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "claveRecaudo": clave,
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-C01",
        "motivo": None,
    }


# --- C02 rechazado --------------------------------------------------------------------------
def test_c02_cobro_rechazado_con_motivo(app_adaptador: Any) -> None:
    respuesta = _cobrar(app_adaptador(), C02)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "RECHAZADO"
    assert respuesta.json()["transaccionId"] is None
    assert respuesta.json()["motivo"] == "FONDOS_INSUFICIENTES"


def test_c02_consulta_rechazado(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), C02)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "RECHAZADO"


# --- C05 malformado -------------------------------------------------------------------------
def test_c05_cobro_malformado_es_502(app_adaptador: Any) -> None:
    _error(_cobrar(app_adaptador(), C05), 502, "RESPUESTA_INVALIDA")


# --- C06 lento: el adaptador corta; la consulta dice que el cobro sí ocurrió -----------------
def test_c06_cobro_lento_agota_el_timeout(app_adaptador: Any) -> None:
    """El simulador tarda 8000 ms; con un timeout de prueba de 1 s el adaptador responde 504."""
    adaptador = app_adaptador(ALLY_PAGOS_TIMEOUT_MS="1000")
    inicio = time.perf_counter()
    respuesta = _cobrar(adaptador, C06)
    duracion_ms = (time.perf_counter() - inicio) * 1000
    _error(respuesta, 504, "TIEMPO_AGOTADO")
    assert 1000 <= duracion_ms < 3000, duracion_ms


def test_c06_consulta_aprobado(app_adaptador: Any) -> None:
    respuesta = _consultar(app_adaptador(), C06)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "APROBADO"
    assert respuesta.json()["transaccionId"] == "TX-SINT-C06"


# --- C07 caído: el cobro nunca llegó --------------------------------------------------------
def test_c07_cobro_caido_es_503(app_adaptador: Any) -> None:
    _error(_cobrar(app_adaptador(), C07), 503, "FUENTE_NO_DISPONIBLE")


def test_c07_consulta_cobro_no_encontrado(app_adaptador: Any) -> None:
    _error(_consultar(app_adaptador(), C07), 404, "COBRO_NO_ENCONTRADO")


# --- Pagador desconocido: el respaldo responde referencia_no_reconocida ----------------------
def test_desconocido_cobro_es_502(app_adaptador: Any) -> None:
    _error(_cobrar(app_adaptador(), DESCONOCIDO), 502, "RESPUESTA_INVALIDA")


def test_desconocido_consulta_es_502(app_adaptador: Any) -> None:
    _error(_consultar(app_adaptador(), DESCONOCIDO), 502, "RESPUESTA_INVALIDA")


# --- Lo que recibe el proveedor y lo que queda en los logs ----------------------------------
def test_journal_del_cobro(app_adaptador: Any, wiremock: Any) -> None:
    clave = str(uuid4())
    assert _cobrar(app_adaptador(), C01, clave).status_code == 200
    (solicitud,) = wiremock.solicitudes()
    cabeceras = {clave.lower(): valor for clave, valor in solicitud["headers"].items()}
    assert cabeceras["idempotency-key"] == clave
    assert cabeceras["x-api-key"] == "local-sintetico"
    assert cabeceras["x-correlation-id"] == CORRELACION
    assert json.loads(solicitud["body"]) == {
        "referencia": clave,
        "monto": "64638.00",
        "moneda": "COP",
        "pagador": C01,
    }


def test_journal_de_la_consulta(app_adaptador: Any, wiremock: Any) -> None:
    clave = str(uuid4())
    assert _consultar(app_adaptador(), C01, clave).status_code == 200
    (solicitud,) = wiremock.solicitudes()
    assert solicitud["method"] == "GET"
    assert solicitud["queryParams"]["referencia"]["values"] == [clave]
    assert solicitud["queryParams"]["pagador"]["values"] == [C01]


@pytest.mark.parametrize("pagador", [C01, C07])
def test_logs_sin_pagador_ni_cuerpos(app_adaptador: Any, pagador: str) -> None:
    adaptador = app_adaptador()
    _cobrar(adaptador, pagador)
    _consultar(adaptador, pagador)
    texto = adaptador.logs.getvalue()
    assert '"operation": "cobro.pagos"' in texto
    assert '"operation": "conciliacion.pagos"' in texto
    # El monto con su punto decimal: solo dígitos podría coincidir con un trace_id (16.1b).
    for centinela in (pagador, "local-sintetico", "64638.00", "TX-SINT"):
        assert centinela not in texto, centinela
