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


# --- HU-W21 (DS3-16): credenciales, fuentes y desfase --------------------------------------------
def test_los_valores_de_hu_w21_por_omision() -> None:
    settings = Settings()
    assert settings.ally_telemetria_credentials is None
    assert settings.hmac_secret is None
    assert settings.telemetria_fuentes == ("IDEAM-SIM", "AEROCIVIL-SIM")
    assert (settings.desfase_max_s, settings.s3_timeout_ms) == (300, 300)
    assert settings.evidencias_bucket_sse == "AES256"
    assert settings.outbox_enabled is False


def test_el_secreto_sale_del_json_de_las_credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLY_TELEMETRIA_CREDENTIALS", '{"hmac_secret": "local-sintetico"}')
    settings = Settings()
    assert settings.hmac_secret == "local-sintetico"
    # El secreto no sale en el repr de la configuración.
    assert "local-sintetico" not in repr(settings)
    assert "local-sintetico" not in str(settings.model_dump())


@pytest.mark.parametrize(
    "credenciales",
    ["", "no es json", "[]", '"texto"', "{}", '{"hmac_secret": ""}', '{"hmac_secret": 1}'],
)
def test_credenciales_ilegibles_o_sin_secreto_no_dan_secreto(
    monkeypatch: pytest.MonkeyPatch, credenciales: str
) -> None:
    monkeypatch.setenv("ALLY_TELEMETRIA_CREDENTIALS", credenciales)
    assert Settings().hmac_secret is None


def test_las_fuentes_son_una_lista_separada_por_comas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEMETRIA_FUENTES", " IDEAM-SIM , OTRA-SIM,, ")
    assert Settings().telemetria_fuentes == ("IDEAM-SIM", "OTRA-SIM")


def test_el_corte_de_s3_no_pasa_de_700_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """§13, regla 7: corte duro de 700 ms."""
    monkeypatch.setenv("SOLVENTA_S3_TIMEOUT_MS", "701")
    with pytest.raises(ValueError):
        Settings()
