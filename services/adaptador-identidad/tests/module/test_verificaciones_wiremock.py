"""Pruebas de módulo: adaptador-identidad contra WireMock con el stub KYC (HU-M06)."""

import time
from typing import Any
from uuid import UUID

import pytest

pytestmark = pytest.mark.usefixtures("wiremock")

CLIENTE = UUID("00000000-0000-4000-8000-0000000000aa")
CORRELACION = "corr-modulo-m06"


def _verificar(adaptador: Any, selfie: str, headers: dict[str, str] | None = None) -> Any:
    return adaptador.cliente.post(
        "/v1/verificaciones",
        json={"clienteId": str(CLIENTE), "selfie": selfie, "pruebaDeVida": {"completado": True}},
        headers={"X-Correlation-Id": CORRELACION, **(headers or {})},
    )


def test_selfie_aprobada(app_adaptador: Any) -> None:
    respuesta = _verificar(app_adaptador(), "selfie-sintetica-aprobada")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {"resultado": "APROBADO", "motivo": None}


def test_captura_invalida_no_es_rechazo_definitivo(app_adaptador: Any) -> None:
    """AC2: el proveedor devuelve una captura inválida, no un rechazo."""
    respuesta = _verificar(app_adaptador(), "selfie-sintetica-captura-invalida")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "resultado": "CAPTURA_INVALIDA",
        "motivo": "ILUMINACION_INSUFICIENTE",
    }


def test_rostro_no_coincide_rechazado(app_adaptador: Any) -> None:
    respuesta = _verificar(app_adaptador(), "selfie-sintetica-rechazada")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {"resultado": "RECHAZADO", "motivo": "ROSTRO_NO_COINCIDE"}


def test_proveedor_caido_es_error_recuperable(app_adaptador: Any) -> None:
    """AC3: comunicación interrumpida -> error recuperable, no un rechazo."""
    respuesta = _verificar(app_adaptador(), "selfie-sintetica-caida")
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "FUENTE_NO_DISPONIBLE"
    assert respuesta.json()["correlationId"] == CORRELACION


def test_proveedor_lento_agota_el_timeout_configurado(app_adaptador: Any) -> None:
    """AC3: el proveedor tarda 5 s; el adaptador corta en su timeout (aquí 300 ms)."""
    adaptador = app_adaptador(ALLY_KYC_TIMEOUT_MS="300")
    inicio = time.perf_counter()
    respuesta = _verificar(adaptador, "selfie-sintetica-lenta")
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert respuesta.status_code == 504
    assert respuesta.json()["codigo"] == "TIEMPO_AGOTADO"
    assert duracion_ms <= 1000, duracion_ms


def test_respuesta_lenta_dentro_del_timeout_no_se_corta(app_adaptador: Any) -> None:
    """Un arranque en frío del proveedor (>700 ms) no es un error si cabe en su timeout."""
    adaptador = app_adaptador(ALLY_KYC_TIMEOUT_MS="8000")
    respuesta = _verificar(adaptador, "selfie-sintetica-lenta")
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["resultado"] == "APROBADO"


def test_selfie_no_reconocida_es_respuesta_invalida(app_adaptador: Any) -> None:
    respuesta = _verificar(app_adaptador(), "selfie-nunca-vista-antes")
    assert respuesta.status_code == 502
    assert respuesta.json()["codigo"] == "RESPUESTA_INVALIDA"


def test_journal_con_correlacion_y_api_key(app_adaptador: Any, wiremock: Any) -> None:
    adaptador = app_adaptador()
    assert _verificar(adaptador, "selfie-sintetica-aprobada").status_code == 200
    (solicitud,) = wiremock.solicitudes()
    cabeceras = {clave.lower(): valor for clave, valor in solicitud["headers"].items()}
    assert cabeceras["x-correlation-id"] == CORRELACION
    assert cabeceras["x-api-key"] == "local-sintetico"


def test_la_selfie_no_queda_en_logs_ni_spans(app_adaptador: Any) -> None:
    """AC4 (back): la selfie nunca se registra, solo se reenvía al proveedor."""
    adaptador = app_adaptador()
    assert _verificar(adaptador, "selfie-sintetica-aprobada").status_code == 200
    assert "selfie-sintetica-aprobada" not in adaptador.logs.getvalue()
    assert "selfie-sintetica-aprobada" not in adaptador.texto_spans()
