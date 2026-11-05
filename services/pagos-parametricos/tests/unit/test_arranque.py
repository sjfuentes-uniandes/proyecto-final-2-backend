"""Arranque de pagos-parametricos con las variables que entrega infra/apps/ecs.tf (HU-S03)."""

import pytest
from pagos_parametricos.config import Settings


def test_sin_infraestructura_usa_los_valores_locales() -> None:
    settings = Settings()
    assert settings.service_name == "pagos-parametricos"
    assert settings.database_name == "pagos_parametricos"
    assert settings.siniestros_url == "http://localhost:8091"
    assert settings.sqs_parametricos_eventos_url is None
    assert settings.sqs_parametricos_polizas_url is None


def test_configuracion_con_los_nombres_de_la_infraestructura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for nombre, valor in {
        "SINIESTROS_URL": "http://siniestros:8080",
        "SQS_PARAMETRICOS_EVENTOS_URL": "https://sqs/solventa-int-parametricos-eventos",
        "SQS_PARAMETRICOS_POLIZAS_URL": "https://sqs/solventa-int-parametricos-polizas",
    }.items():
        monkeypatch.setenv(nombre, valor)
    settings = Settings()
    assert settings.siniestros_url == "http://siniestros:8080"
    assert settings.sqs_parametricos_eventos_url.endswith("parametricos-eventos")
    assert settings.sqs_parametricos_polizas_url.endswith("parametricos-polizas")
