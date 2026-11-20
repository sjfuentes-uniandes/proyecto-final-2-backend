"""Arranque de siniestros con las variables que entrega infra/apps/ecs.tf (HU-S03)."""

import pytest
from pydantic import ValidationError
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


def test_valores_por_omision_de_la_historia_w19() -> None:
    settings = Settings()
    assert settings.evidencia_url_ttl_s == 300
    assert settings.s3_timeout_ms == 300
    assert settings.compromiso_horas == 48
    assert settings.prioridad_alta_min == 8 * 60
    assert settings.prioridad_media_min == 24 * 60
    assert settings.coherencia_distancia_max_m == 1000
    # La semilla de QA está apagada: solo local e INT la encienden.
    assert settings.semilla_qa is False


def test_la_semilla_de_qa_se_enciende_con_su_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_SEMILLA_SINIESTROS_QA", "true")
    assert Settings().semilla_qa is True


@pytest.mark.parametrize("ttl", ["0", "301", "3600"])
def test_la_url_de_evidencia_no_vive_mas_de_300_segundos(
    monkeypatch: pytest.MonkeyPatch, ttl: str
) -> None:
    monkeypatch.setenv("SOLVENTA_EVIDENCIA_URL_TTL_S", ttl)
    with pytest.raises(ValidationError):
        Settings()


def test_un_ttl_menor_es_valido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVIDENCIA_URL_TTL_S", "60")
    assert Settings().evidencia_url_ttl_s == 60


def test_los_umbrales_de_prioridad_deben_estar_ordenados(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_PRIORIDAD_ALTA_MIN", "1440")
    monkeypatch.setenv("SOLVENTA_PRIORIDAD_MEDIA_MIN", "480")
    with pytest.raises(ValidationError, match="prioridad_alta_min"):
        Settings()


def test_el_corte_de_s3_tiene_un_techo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_S3_TIMEOUT_MS", "5001")
    with pytest.raises(ValidationError):
        Settings()
