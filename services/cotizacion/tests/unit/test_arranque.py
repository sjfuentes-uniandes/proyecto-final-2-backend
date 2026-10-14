from typing import Any

import psycopg_pool
import pytest
from cotizacion.adapters.outbound import perfiles_pg
from cotizacion.adapters.outbound.adaptador_datos_http import AdaptadorDatosHttp
from cotizacion.adapters.outbound.catalogo_http import CatalogoHttp
from cotizacion.adapters.outbound.clientes_http import ClientesHttp
from cotizacion.adapters.outbound.perfiles_pg import PerfilesPg
from cotizacion.application.perfilamiento.crear_perfil import CrearPerfil
from cotizacion.application.perfilamiento.ejecutor_fuentes import EjecutorSecuencial
from cotizacion.config import Settings
from cotizacion.main import crear_app
from fastapi.testclient import TestClient
from pydantic import ValidationError


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
def entorno_limpio(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for variable in (
        "SOLVENTA_CLIENTES_URL",
        "CLIENTES_URL",
        "CATALOGO_URL",
        "ADAPTADOR_DATOS_URL",
    ):
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


def test_cableado_con_base_configurada(entorno_limpio: pytest.MonkeyPatch) -> None:
    migraciones: list[str] = []
    entorno_limpio.setattr(perfiles_pg, "migrar", migraciones.append)
    entorno_limpio.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)
    settings = Settings(
        DB_HOST="rds.interno",
        DB_POOL_SIZE=7,
        CATALOGO_URL="http://catalogo:8080",
        ADAPTADOR_DATOS_URL="http://adaptador-datos:8080",
        dependencia_timeout_ms=120,
        fuente_timeout_ms=250,
        perfilamiento_presupuesto_ms=380,
    )
    with TestClient(crear_app(settings)) as cliente:
        estado = cliente.app.state  # type: ignore[attr-defined]
        caso: CrearPerfil = estado.crear_perfil
        pool = estado.pool
        assert isinstance(caso._identidad, ClientesHttp) and caso._identidad is caso._consentimiento
        assert isinstance(caso._modelos, CatalogoHttp)
        assert isinstance(caso._ejecutor, EjecutorSecuencial)
        assert isinstance(caso._ejecutor._puerto, AdaptadorDatosHttp)
        assert isinstance(caso._repo, PerfilesPg) and estado.perfiles is caso._repo
        assert (caso.config.dependencia_timeout_ms, caso.config.fuente_timeout_ms) == (120, 250)
        assert caso.config.presupuesto_ms == 380
        assert caso._modelos._base_url == "http://catalogo:8080"
        assert caso._ejecutor._puerto._url == "http://adaptador-datos:8080/v1/consultas"
        assert caso._identidad._base_url == "http://localhost:8089/clientes-stub"
        assert estado.reconstruir_perfil is not None
    assert migraciones == [settings.database_url]
    assert pool.argumentos == {"min_size": 1, "max_size": 7, "open": False}
    assert pool.abierto is False


def test_sin_base_no_hay_casos_de_uso(entorno_limpio: pytest.MonkeyPatch) -> None:
    with TestClient(crear_app(Settings())) as cliente:
        assert not hasattr(cliente.app.state, "crear_perfil")  # type: ignore[attr-defined]
        assert cliente.get("/health").json() == {"status": "ok"}


def test_dt_02_el_stub_tiene_prioridad(entorno_limpio: pytest.MonkeyPatch) -> None:
    entorno_limpio.setenv("CLIENTES_URL", "http://clientes:8080")
    assert Settings().clientes_url == "http://clientes:8080"
    entorno_limpio.setenv("SOLVENTA_CLIENTES_URL", "http://simulador-aliados:8080/clientes-stub")
    assert Settings().clientes_url == "http://simulador-aliados:8080/clientes-stub"


def test_la_rama_de_una_fuente_no_pasa_de_700_ms(entorno_limpio: pytest.MonkeyPatch) -> None:
    entorno_limpio.setenv("SOLVENTA_FUENTE_TIMEOUT_MS", "701")
    with pytest.raises(ValidationError):
        Settings()


class _Conexion:
    def __init__(self, falla: bool) -> None:
        self.falla = falla

    async def execute(self, sql: str) -> None:
        if self.falla:
            raise OSError("sin conexión")


class _Pool:
    def __init__(self, falla: bool) -> None:
        self.falla = falla

    def connection(self) -> Any:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def abrir() -> Any:
            yield _Conexion(self.falla)

        return abrir()


@pytest.mark.parametrize(("falla", "estado"), [(False, 200), (True, 503)])
def test_health_verifica_la_base(falla: bool, estado: int) -> None:
    with TestClient(crear_app(Settings())) as cliente:
        cliente.app.state.pool = _Pool(falla)  # type: ignore[attr-defined]
        assert cliente.get("/health").status_code == estado
