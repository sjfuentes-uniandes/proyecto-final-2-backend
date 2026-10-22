from typing import Any, Self

import psycopg_pool
import pytest
from fastapi.testclient import TestClient
from polizas.adapters.outbound import decisiones_pg
from polizas.config import Settings
from polizas.main import crear_app


class PoolFalso:
    def __init__(self, url: str, **argumentos: Any) -> None:
        self.url = url
        self.argumentos = argumentos
        self.abierto = False
        self.sano = True

    async def open(self) -> None:
        self.abierto = True

    async def close(self) -> None:
        self.abierto = False

    def connection(self) -> Any:
        pool = self

        class Conexion:
            async def __aenter__(self) -> Self:
                if not pool.sano:
                    raise OSError("base caída")
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def execute(self, sql: str) -> None:
                return None

        return Conexion()


@pytest.mark.parametrize("con_base", [True, False])
def test_el_arranque_migra_y_abre_el_pool_solo_con_base_configurada(
    monkeypatch: pytest.MonkeyPatch, con_base: bool
) -> None:
    migraciones: list[str] = []
    monkeypatch.setattr(decisiones_pg, "migrar", migraciones.append)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    settings = Settings(DB_HOST="rds.interno" if con_base else None, DB_POOL_SIZE=7)
    with TestClient(crear_app(settings)) as cliente:
        estado = cliente.app.state  # type: ignore[attr-defined]
        assert hasattr(estado, "solicitar_suscripcion") is con_base
        assert hasattr(estado, "consultar_suscripciones") is con_base
        assert hasattr(estado, "reconstruir_decision") is con_base
        pool = getattr(estado, "pool", None)
        assert cliente.get("/health").json() == {"status": "ok"}
        if con_base:
            pool.sano = False
            caida = cliente.get("/health")
            assert (caida.status_code, caida.json()) == (503, {"status": "base no disponible"})
    assert len(migraciones) == (1 if con_base else 0)
    if con_base:
        assert "rds.interno" in migraciones[0]
        assert pool.argumentos == {"min_size": 1, "max_size": 7, "open": False}
        assert pool.abierto is False  # se cerró al terminar el lifespan


def test_reloj_del_sistema_en_utc() -> None:
    from polizas.adapters.outbound.reloj import RelojSistema

    assert RelojSistema().ahora().utcoffset() is not None


def test_configuracion_por_omision_y_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """DS2-19: timeouts bajo el corte de 700 ms; URL con el nombre de la infraestructura."""
    monkeypatch.setenv("COTIZACION_URL", "http://cotizacion:8080")
    monkeypatch.setenv("SOLVENTA_CATALOGO_URL", "http://catalogo:8080")
    settings = Settings()
    assert (settings.cotizacion_url, settings.catalogo_url) == (
        "http://cotizacion:8080",
        "http://catalogo:8080",
    )
    assert (settings.cotizacion_timeout_ms, settings.catalogo_timeout_ms) == (600, 300)
    assert settings.compromiso_revision_horas == 8
    assert settings.database_name == "polizas"
    with pytest.raises(ValueError):
        Settings(cotizacion_timeout_ms=701)
