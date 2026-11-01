"""Pruebas de módulo: adaptador-firma contra WireMock con los mapeos de firma (HU-W17, 17.3).

El firmante sintético elige el caso (services/simulador-aliados/README.md, «Firma por cliente
sintético»).
"""

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

C01 = "00000000-0000-4000-8000-000000000001"  # firmado
C05 = "00000000-0000-4000-8000-000000000005"  # malformado
C06 = "00000000-0000-4000-8000-000000000006"  # lento (8000 ms), la firma sí ocurre
C07 = "00000000-0000-4000-8000-000000000007"  # caído
DESCONOCIDO = "00000000-0000-4000-8000-0000000000ff"
CORRELACION = "corr-modulo-w17"


def _huella() -> str:
    return "sha256:" + hashlib.sha256(uuid4().bytes).hexdigest()


def _firmar(
    adaptador: Any, firmante: str, documento: str | None = None, huella: str | None = None
) -> Any:
    return adaptador.cliente.post(
        "/v1/firmas",
        json={
            "documentoId": documento or str(uuid4()),
            "huella": huella or _huella(),
            "firmanteRef": firmante,
        },
        headers={"X-Correlation-Id": CORRELACION},
    )


def _error(respuesta: Any, estado: int, codigo: str) -> None:
    assert respuesta.status_code == estado, respuesta.text
    assert respuesta.json()["codigo"] == codigo
    assert respuesta.json()["correlationId"] == CORRELACION


def test_c01_firmado_devuelve_el_documento_y_la_huella(app_adaptador: Any) -> None:
    documento, huella = str(uuid4()), _huella()
    respuesta = _firmar(app_adaptador(), C01, documento, huella)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "documentoId": documento,
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
        "huella": huella,
    }


def test_c05_malformado_es_502(app_adaptador: Any) -> None:
    _error(_firmar(app_adaptador(), C05), 502, "RESPUESTA_INVALIDA")


def test_c06_lento_agota_el_timeout(app_adaptador: Any) -> None:
    """El simulador tarda 8000 ms; con un timeout de prueba de 1 s el adaptador responde 504."""
    adaptador = app_adaptador(ALLY_FIRMA_TIMEOUT_MS="1000")
    inicio = time.perf_counter()
    respuesta = _firmar(adaptador, C06)
    duracion_ms = (time.perf_counter() - inicio) * 1000
    _error(respuesta, 504, "TIEMPO_AGOTADO")
    assert 1000 <= duracion_ms < 3000, duracion_ms


def test_c07_caido_es_503(app_adaptador: Any) -> None:
    _error(_firmar(app_adaptador(), C07), 503, "FUENTE_NO_DISPONIBLE")


def test_desconocido_es_502(app_adaptador: Any) -> None:
    """El comodín del simulador responde 404 `documento_no_reconocido`: no es un rechazo."""
    _error(_firmar(app_adaptador(), DESCONOCIDO), 502, "RESPUESTA_INVALIDA")


# --- Lo que recibe el proveedor y lo que queda en los logs ----------------------------------
def test_journal_de_la_firma(app_adaptador: Any, wiremock: Any) -> None:
    documento, huella = str(uuid4()), _huella()
    assert _firmar(app_adaptador(), C01, documento, huella).status_code == 200
    (solicitud,) = wiremock.solicitudes()
    assert solicitud["method"] == "POST"
    assert solicitud["url"] == "/firma/v1/firmas"
    cabeceras = {clave.lower(): valor for clave, valor in solicitud["headers"].items()}
    assert cabeceras["idempotency-key"] == documento
    assert cabeceras["x-api-key"] == "local-sintetico"
    assert cabeceras["x-correlation-id"] == CORRELACION
    assert json.loads(solicitud["body"]) == {
        "documentoId": documento,
        "huella": huella,
        "firmante": C01,
    }


@pytest.mark.parametrize("firmante", [C01, C05, C07])
def test_logs_sin_firmante_ni_cuerpos(app_adaptador: Any, firmante: str) -> None:
    adaptador = app_adaptador()
    huella = _huella()
    _firmar(adaptador, firmante, huella=huella)
    texto = adaptador.logs.getvalue()
    assert '"operation": "firma.documento"' in texto
    # El documentoId y el firmaId sí pueden ir a los logs; la huella y el firmante, no.
    for centinela in (firmante, "local-sintetico", huella, huella[7:20]):
        assert centinela not in texto, centinela
