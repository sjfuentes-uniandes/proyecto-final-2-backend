"""Cableado del pool asíncrono de consentimientos en el ciclo de vida de la app (PR 1.4)."""

from typing import Any

import httpx
import pytest
from clientes.adapters.outbound import postgres
from clientes.adapters.outbound.consentimientos_pg import ConsentimientosPg
from clientes.config import Settings
from clientes.main import crear_app
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _app(settings: Settings) -> Any:
    return crear_app(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200)))


def test_sin_repositorios_inyectados_abre_el_pool_y_lo_cierra_al_apagar(
    monkeypatch: pytest.MonkeyPatch, pools: list[Any]
) -> None:
    monkeypatch.setattr(postgres, "migrar", lambda _: None)
    settings = Settings(db_pool_min_size=2, db_pool_size=7)
    app = _app(settings)

    with TestClient(app):
        (pool,) = pools
        assert pool.eventos == ["open"]
        assert pool.conninfo == settings.database_url
        assert pool.opciones == {"min_size": 2, "max_size": 7, "open": False}
        repositorio = app.state.otorgar_consentimiento._repositorio
        assert isinstance(repositorio, ConsentimientosPg)
        assert repositorio._pool is pool

    assert pool.eventos == ["open", "close"]


def test_un_repositorio_de_consentimientos_inyectado_no_abre_pool(
    monkeypatch: pytest.MonkeyPatch, pools: list[Any], consentimientos: Any
) -> None:
    monkeypatch.setattr(postgres, "migrar", lambda _: None)
    app = crear_app(
        Settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        repositorio_consentimientos=consentimientos,
    )

    with TestClient(app):
        assert app.state.consultar_consentimientos._repositorio is consentimientos

    assert pools == []


def test_con_solo_el_repositorio_de_identidad_no_cablea_consentimientos(
    repositorio: Any, pools: list[Any]
) -> None:
    app = crear_app(
        Settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        repositorio=repositorio,
    )

    with TestClient(app):
        assert not hasattr(app.state, "otorgar_consentimiento")

    assert pools == []


def test_el_pool_se_dimensiona_con_el_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "3")
    monkeypatch.setenv("DB_POOL_SIZE", "12")

    settings = Settings()

    assert (settings.db_pool_min_size, settings.db_pool_size) == (3, 12)


def test_el_pool_por_defecto() -> None:
    settings = Settings()

    assert (settings.db_pool_min_size, settings.db_pool_size) == (1, 5)


@pytest.mark.parametrize(("minimo", "maximo"), [(0, 5), (6, 5)])
def test_rechaza_un_pool_mal_dimensionado(minimo: int, maximo: int) -> None:
    with pytest.raises(ValidationError):
        Settings(db_pool_min_size=minimo, db_pool_size=maximo)
