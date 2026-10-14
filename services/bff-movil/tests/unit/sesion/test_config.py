"""Configuración (A6): nombres de la infraestructura, variante SOLVENTA_ y valores por defecto."""

import pytest
from bff_movil.config import Settings


def test_valores_por_defecto_seguros(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in ("CLIENTES_URL", "JWT_ISSUERS", "ENVIRONMENT"):
        monkeypatch.delenv(nombre, raising=False)

    settings = Settings()

    assert settings.clientes_url == "http://localhost:8083"
    assert settings.jwt_issuers == {}
    assert settings.clientes_timeout_ms == 500
    assert settings.jwks_ttl_s == 3600
    assert settings.jwks_timeout_ms == 500
    assert settings.jwks_refresco_min_s == 60


@pytest.mark.parametrize("nombre", ["CLIENTES_URL", "SOLVENTA_CLIENTES_URL"])
def test_acepta_la_url_de_clientes_con_y_sin_prefijo(
    monkeypatch: pytest.MonkeyPatch, nombre: str
) -> None:
    monkeypatch.setenv(nombre, "http://clientes:8080")

    assert Settings().clientes_url == "http://clientes:8080"


@pytest.mark.parametrize("nombre", ["JWT_ISSUERS", "SOLVENTA_JWT_ISSUERS"])
def test_acepta_los_emisores_como_json(monkeypatch: pytest.MonkeyPatch, nombre: str) -> None:
    monkeypatch.setenv(nombre, '{"customers": "https://emisor.example/clientes"}')

    assert Settings().jwt_issuers == {"customers": "https://emisor.example/clientes"}


def test_las_variables_propias_usan_el_prefijo_solventa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_CLIENTES_TIMEOUT_MS", "300")
    monkeypatch.setenv("SOLVENTA_JWKS_TTL_S", "120")

    settings = Settings()

    assert settings.clientes_timeout_ms == 300
    assert settings.jwks_ttl_s == 120
