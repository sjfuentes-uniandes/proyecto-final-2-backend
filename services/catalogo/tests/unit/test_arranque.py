from typing import Any

import psycopg_pool
import pytest
from catalogo.adapters.outbound import modelos_pg
from catalogo.config import Settings
from catalogo.main import crear_app
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


@pytest.mark.parametrize("con_base", [True, False])
def test_el_arranque_migra_y_abre_el_pool_solo_con_base_configurada(
    monkeypatch: pytest.MonkeyPatch, con_base: bool
) -> None:
    migraciones: list[str] = []
    monkeypatch.setattr(modelos_pg, "migrar", migraciones.append)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    settings = Settings(DB_HOST="rds.interno" if con_base else None, DB_POOL_SIZE=7)
    with TestClient(crear_app(settings)) as cliente:
        estado = cliente.app.state  # type: ignore[attr-defined]
        assert hasattr(estado, "consultar_modelo") is con_base
        assert hasattr(estado, "consultar_reglas_suscripcion") is con_base
        pool = getattr(estado, "pool", None)
    assert len(migraciones) == (1 if con_base else 0)
    if con_base:
        assert "rds.interno" in migraciones[0]
        assert pool.url == settings.database_url
        assert pool.argumentos == {"min_size": 1, "max_size": 7, "open": False}
        assert pool.abierto is False  # se cerró al terminar el lifespan
