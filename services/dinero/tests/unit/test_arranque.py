"""Configuración y ciclo de vida de dinero (HU-W16, 16.9): migraciones y pool de la base."""

from typing import Any

import psycopg_pool
import pytest
from dinero.adapters.outbound import recaudos_pg
from dinero.config import Settings
from dinero.main import crear_app
from fastapi.testclient import TestClient


class PoolFalso:
    def __init__(self, url: str, **argumentos: Any) -> None:
        self.url = url
        self.argumentos = argumentos
        self.abierto = False

    async def open(self) -> None:
        self.abierto = True

    async def close(self) -> None:
        self.abierto = False


@pytest.fixture
def migraciones(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    aplicadas: list[str] = []
    monkeypatch.setattr(recaudos_pg, "migrar", aplicadas.append)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    return aplicadas


def test_con_base_migra_abre_el_pool_y_lo_cierra(migraciones: list[str]) -> None:
    settings = Settings(DB_HOST="rds.interno", DB_POOL_SIZE=7)
    with TestClient(crear_app(settings)) as cliente:
        pool = cliente.app.state.pool  # type: ignore[attr-defined]
        assert isinstance(pool, PoolFalso)
        assert pool.abierto is True
        assert cliente.get("/health").json() == {"status": "ok"}
    assert len(migraciones) == 1
    assert "@rds.interno:5432/dinero?" in migraciones[0]
    assert migraciones[0] == pool.url
    assert pool.argumentos == {"min_size": 1, "max_size": 7, "open": False}
    assert pool.abierto is False  # se cerró al terminar el lifespan


def test_la_url_de_la_base_tambien_activa_el_pool(migraciones: list[str]) -> None:
    url = "postgresql://u:p@localhost:5432/dinero"
    with TestClient(crear_app(Settings(SOLVENTA_DATABASE_URL=url))) as cliente:
        assert cliente.app.state.pool.url == url  # type: ignore[attr-defined]
    assert migraciones == [url]


def test_sin_base_no_migra_ni_abre_pool(migraciones: list[str]) -> None:
    with TestClient(crear_app(Settings())) as cliente:
        assert getattr(cliente.app.state, "pool", None) is None  # type: ignore[attr-defined]
        assert cliente.get("/health").json() == {"status": "ok"}
    assert migraciones == []


def test_un_pool_inyectado_se_usa_tal_cual_y_no_migra(migraciones: list[str]) -> None:
    """Costura de las pruebas: la base ya está migrada y el pool lo abre y cierra la prueba."""
    inyectado = PoolFalso("postgresql://prueba")
    settings = Settings(DB_HOST="rds.interno")
    with TestClient(crear_app(settings, pool=inyectado)) as cliente:  # type: ignore[arg-type]
        assert cliente.app.state.pool is inyectado  # type: ignore[attr-defined]
    assert migraciones == []
    assert inyectado.abierto is False  # nunca se abrió ni se cerró aquí


def test_configuracion_con_los_nombres_de_la_infraestructura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """infra/apps/ecs.tf entrega a dinero (database = true) DB_HOST, DB_PORT, DB_NAME, DB_SSLMODE y
    DB_POOL_SIZE, y DB_USER y DB_PASSWORD como secretos."""
    for nombre, valor in {
        "DB_HOST": "rds.interno",
        "DB_PORT": "5433",
        "DB_NAME": "dinero",
        "DB_SSLMODE": "require",
        "DB_POOL_SIZE": "9",
        "DB_USER": "dinero",
        "DB_PASSWORD": "s3cr3t@",
    }.items():
        monkeypatch.setenv(nombre, valor)
    settings = Settings()
    assert settings.service_name == "dinero"
    assert settings.db_pool_size == 9
    assert settings.database_url == (
        "postgresql://dinero:s3cr3t%40@rds.interno:5433/dinero?sslmode=require"
    )
    assert "s3cr3t" not in repr(settings)


def test_configuracion_por_omision() -> None:
    settings = Settings()
    assert settings.database_name == "dinero"
    assert settings.db_host is None


def test_reloj_del_sistema_en_utc() -> None:
    from dinero.adapters.outbound.reloj import RelojSistema

    assert RelojSistema().ahora().utcoffset() is not None
