"""Módulo: verificación documental de adaptador-identidad contra WireMock (HU-W30).

Perfiles del proveedor: nominal, rechazado, lento, malformado y caído.
"""

import time
from typing import Any
from uuid import UUID

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

CLIENTE = UUID("00000000-0000-4000-8000-0000000000bb")
CORRELACION = "corr-modulo-w30"


def _verificar(adaptador: Any, numero_documento: str) -> Any:
    return adaptador.cliente.post(
        "/v1/verificaciones/documento",
        json={
            "clienteId": str(CLIENTE),
            "tipoDocumento": "CC",
            "numeroDocumento": numero_documento,
            "nombreCompleto": "Natalia González Ruiz",
            "fechaNacimiento": "1990-04-18",
        },
        headers={"X-Correlation-Id": CORRELACION},
    )


def test_nominal_aprobado_con_referencia(app_adaptador: Any) -> None:
    respuesta = _verificar(app_adaptador(), "1020456789")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "resultado": "APROBADO",
        "motivo": None,
        "referencia": "kyc-doc-ref-0001",
        "proveedor": "KYC",
    }


def test_rechazado_por_el_proveedor(app_adaptador: Any) -> None:
    respuesta = _verificar(app_adaptador(), "1000000002")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["resultado"] == "RECHAZADO"
    assert respuesta.json()["referencia"] == "kyc-doc-ref-0002"


def test_proveedor_lento_agota_el_timeout(app_adaptador: Any) -> None:
    adaptador = app_adaptador(ALLY_KYC_TIMEOUT_MS="300")
    inicio = time.perf_counter()
    respuesta = _verificar(adaptador, "1000000003")
    assert (time.perf_counter() - inicio) * 1000 <= 1000
    assert respuesta.status_code == 504
    assert respuesta.json()["codigo"] == "TIEMPO_AGOTADO"


def test_respuesta_malformada_es_respuesta_invalida(app_adaptador: Any) -> None:
    respuesta = _verificar(app_adaptador(), "1000000004")
    assert respuesta.status_code == 502
    assert respuesta.json()["codigo"] == "RESPUESTA_INVALIDA"


def test_proveedor_caido_es_error_recuperable(app_adaptador: Any) -> None:
    respuesta = _verificar(app_adaptador(), "1000000005")
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "FUENTE_NO_DISPONIBLE"
    assert respuesta.json()["correlationId"] == CORRELACION


def test_el_proveedor_recibe_datos_de_documento_sin_biometria(
    app_adaptador: Any, wiremock: Any
) -> None:
    _verificar(app_adaptador(), "1020456789")
    (solicitud,) = [s for s in wiremock.solicitudes() if s["url"].endswith("/documento")]
    assert "selfie" not in solicitud["body"]
    assert "pruebaDeVida" not in solicitud["body"]
