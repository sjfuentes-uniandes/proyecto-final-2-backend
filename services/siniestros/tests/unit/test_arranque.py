"""Arranque de siniestros con las variables que entrega infra/apps/ecs.tf (HU-S03)."""

import pytest
from siniestros.config import Settings


def test_sin_infraestructura_usa_los_valores_locales() -> None:
    settings = Settings()
    assert settings.service_name == "siniestros"
    assert settings.database_name == "siniestros"
    assert settings.evidencias_bucket is None
    assert settings.sqs_siniestros_indemnizaciones_url is None


def test_configuracion_con_los_nombres_de_la_infraestructura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for nombre, valor in {
        "DB_HOST": "rds.interno",
        "DB_NAME": "siniestros",
        "EVIDENCIAS_BUCKET": "solventa-int-evidencias-123456789012",
        "EVIDENCIAS_BUCKET_SSE": "aws:kms",
        "SQS_SINIESTROS_INDEMNIZACIONES_URL": "https://sqs/solventa-int-siniestros-indemnizaciones",
    }.items():
        monkeypatch.setenv(nombre, valor)
    settings = Settings()
    assert "@rds.interno:5432/siniestros?" in settings.database_url
    assert settings.evidencias_bucket == "solventa-int-evidencias-123456789012"
    assert settings.evidencias_bucket_sse == "aws:kms"
    assert settings.sqs_siniestros_indemnizaciones_url.endswith("siniestros-indemnizaciones")
