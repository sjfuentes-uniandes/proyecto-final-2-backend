"""Arranque de adaptador-telemetria con las variables que entrega infra/apps/ecs.tf (HU-S03)."""

import pytest
from adaptador_telemetria.config import Settings


def test_sin_infraestructura_usa_los_valores_locales() -> None:
    settings = Settings()
    assert settings.service_name == "adaptador-telemetria"
    # Base propia para el Outbox (ConexionTelemetria --> PublicadoresOutbox).
    assert settings.database_name == "adaptador_telemetria"
    assert settings.evidencias_bucket is None
    assert settings.telemetria_archivo_prefijo == "telemetria/"


def test_configuracion_con_los_nombres_de_la_infraestructura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCIAS_BUCKET", "solventa-int-evidencias-123456789012")
    monkeypatch.setenv("TELEMETRIA_ARCHIVO_PREFIJO", "telemetria/")
    settings = Settings()
    assert settings.evidencias_bucket == "solventa-int-evidencias-123456789012"
    assert settings.telemetria_archivo_prefijo == "telemetria/"
