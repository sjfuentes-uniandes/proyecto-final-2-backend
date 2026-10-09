import pytest
from solventa_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "api-socios"


@pytest.fixture(autouse=True)
def entorno_limpio(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE",
                 "ENVIRONMENT", "SOLVENTA_DATABASE_URL", "SOLVENTA_DB_HOST"]:
        monkeypatch.delenv(name, raising=False)


def test_valores_locales_por_defecto() -> None:
    settings = Settings()
    assert settings.environment == "local"
    assert settings.database_url == "postgresql://solventa:solventa@localhost:5432/api_socios?sslmode=prefer"


def test_contrato_de_infra_en_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    # Variables que entrega infra/apps (ecs.tf); DB_USER/DB_PASSWORD vienen de Secrets Manager.
    monkeypatch.setenv("ENVIRONMENT", "int")
    monkeypatch.setenv("DB_HOST", "solventa-int.abc.us-east-1.rds.amazonaws.com")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "api_socios")
    monkeypatch.setenv("DB_USER", "api_socios")
    monkeypatch.setenv("DB_PASSWORD", "p@ss/word")
    monkeypatch.setenv("DB_SSLMODE", "require")
    settings = Settings()
    assert settings.environment == "int"
    assert settings.database_url == (
        "postgresql://api_socios:p%40ss%2Fword@solventa-int.abc.us-east-1.rds.amazonaws.com:5432/"
        "api_socios?sslmode=require"
    )


def test_url_explicita_tiene_prioridad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_HOST", "rds")
    monkeypatch.setenv("SOLVENTA_DATABASE_URL", "postgresql://u:p@postgres:5432/x")
    assert Settings().database_url == "postgresql://u:p@postgres:5432/x"


def test_password_no_aparece_en_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "secreto-123")
    assert "secreto-123" not in repr(Settings())
