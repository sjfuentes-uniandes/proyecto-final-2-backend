"""Pruebas de módulo: adaptador-firma contra WireMock con los mapeos de push (HU-M11).

El token sintético del dispositivo elige el caso (services/simulador-aliados/README.md,
«Pagos, firma y push»).
"""

import json
import time
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

ENTREGADO = "token-sintetico-entregado"
LENTO = "token-sintetico-lento"
CAIDO = "token-sintetico-caido"
MALFORMADO = "token-sintetico-malformado"
DESCONOCIDO = "token-sintetico-desconocido"
CORRELACION = "corr-modulo-m11"


def _notificar(adaptador: Any, token: str) -> Any:
    return adaptador.cliente.post(
        "/v1/notificaciones",
        json={
            "tokenDispositivo": token,
            "tipo": "POLIZA_EMITIDA",
            "polizaId": str(uuid4()),
            "eventId": str(uuid4()),
        },
        headers={"X-Correlation-Id": CORRELACION},
    )


def _error(respuesta: Any, estado: int, codigo: str) -> None:
    assert respuesta.status_code == estado, respuesta.text
    assert respuesta.json()["codigo"] == codigo
    assert respuesta.json()["correlationId"] == CORRELACION


def test_entregado_devuelve_ok_y_el_envioId(app_adaptador: Any) -> None:
    respuesta = _notificar(app_adaptador(), ENTREGADO)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {"resultado": "OK", "envioId": "PUSH-SINT-0001"}


def test_malformado_es_502(app_adaptador: Any) -> None:
    _error(_notificar(app_adaptador(), MALFORMADO), 502, "RESPUESTA_INVALIDA")


def test_lento_agota_el_timeout(app_adaptador: Any) -> None:
    """El simulador tarda 4000 ms; con un timeout de prueba de 1 s el adaptador responde 504."""
    adaptador = app_adaptador(ALLY_PUSH_TIMEOUT_MS="1000")
    inicio = time.perf_counter()
    respuesta = _notificar(adaptador, LENTO)
    duracion_ms = (time.perf_counter() - inicio) * 1000
    _error(respuesta, 504, "TIEMPO_AGOTADO")
    assert 1000 <= duracion_ms < 3000, duracion_ms


def test_caido_es_503(app_adaptador: Any) -> None:
    _error(_notificar(app_adaptador(), CAIDO), 503, "FUENTE_NO_DISPONIBLE")


def test_desconocido_es_rechazado(app_adaptador: Any) -> None:
    """El comodín del simulador responde 404 `token_no_reconocido`: sí es un rechazo (HU-M11)."""
    respuesta = _notificar(app_adaptador(), DESCONOCIDO)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {"resultado": "RECHAZADO", "envioId": None}


# --- Lo que recibe el proveedor y lo que queda en los logs ----------------------------------
def test_journal_del_push(app_adaptador: Any, wiremock: Any) -> None:
    poliza_id, event_id = str(uuid4()), str(uuid4())
    adaptador = app_adaptador()
    respuesta = adaptador.cliente.post(
        "/v1/notificaciones",
        json={
            "tokenDispositivo": ENTREGADO,
            "tipo": "POLIZA_EMITIDA",
            "polizaId": poliza_id,
            "eventId": event_id,
        },
        headers={"X-Correlation-Id": CORRELACION},
    )
    assert respuesta.status_code == 200
    (solicitud,) = wiremock.solicitudes()
    assert solicitud["method"] == "POST"
    assert solicitud["url"] == "/push/v1/envios"
    cabeceras = {clave.lower(): valor for clave, valor in solicitud["headers"].items()}
    assert cabeceras["x-api-key"] == "local-sintetico"
    assert cabeceras["x-correlation-id"] == CORRELACION
    assert json.loads(solicitud["body"]) == {
        "tokenDispositivo": ENTREGADO,
        "tipo": "POLIZA_EMITIDA",
        "polizaId": poliza_id,
        "eventId": event_id,
    }


@pytest.mark.parametrize("token", [ENTREGADO, CAIDO])
def test_logs_sin_el_token(app_adaptador: Any, token: str) -> None:
    adaptador = app_adaptador()
    _notificar(adaptador, token)
    texto = adaptador.logs.getvalue()
    assert '"operation": "notificacion.push"' in texto
    assert token not in texto
    assert "local-sintetico" not in texto
