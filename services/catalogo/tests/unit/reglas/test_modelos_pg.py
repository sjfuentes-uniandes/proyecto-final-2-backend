from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Self

import psycopg
import pytest
from catalogo.adapters.outbound import modelos_pg
from catalogo.adapters.outbound.modelos_pg import ModelosPg, definicion_semilla, migrar
from catalogo.adapters.outbound.reloj import RelojSistema
from catalogo.domain.reglas.errores import VersionExistente
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.modelo import ModeloPublicado

pytestmark = pytest.mark.anyio
VIGENTE = datetime(2026, 10, 1, tzinfo=UTC)


class ConexionSincrona:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []
        self.argumentos: dict[str, Any] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))


def test_migrar_bloquea_aplica_los_sql_siembra_y_libera(monkeypatch: pytest.MonkeyPatch) -> None:
    conexion = ConexionSincrona()

    def conectar(url: str, **argumentos: Any) -> ConexionSincrona:
        conexion.argumentos = {"url": url, **argumentos}
        return conexion

    monkeypatch.setattr(modelos_pg.psycopg, "connect", conectar)
    migrar("postgresql://u@h/catalogo")
    sentencias = [sql for sql, _ in conexion.sentencias]
    assert conexion.argumentos == {"url": "postgresql://u@h/catalogo", "autocommit": True}
    assert "pg_advisory_lock(hashtext('catalogo.migraciones'))" in sentencias[0]
    assert "CREATE TABLE IF NOT EXISTS modelos_riesgo" in sentencias[1]
    assert "ON CONFLICT (version) DO NOTHING" in sentencias[2]
    assert "pg_advisory_unlock" in sentencias[-1]
    version, producto, desde, definicion, hash_, actor = conexion.sentencias[2][1]
    assert (version, producto, desde, actor) == (
        "2026.10.1",
        "vida-hipotecario",
        VIGENTE,
        "migracion",
    )
    assert definicion.obj == definicion_semilla()
    assert hash_ == hash_canonico(definicion_semilla())


def test_migrar_libera_el_lock_si_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fallida(ConexionSincrona):
        def execute(self, sql: str, parametros: Any = None) -> None:
            super().execute(sql, parametros)
            if "CREATE TABLE" in sql:
                raise psycopg.errors.SyntaxError("falla")

    conexion = Fallida()
    monkeypatch.setattr(modelos_pg.psycopg, "connect", lambda url, **_: conexion)
    with pytest.raises(psycopg.errors.SyntaxError):
        migrar("postgresql://u@h/catalogo")
    assert "pg_advisory_unlock" in conexion.sentencias[-1][0]


class Cursor:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.fila = fila
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))

    async def fetchone(self) -> dict[str, Any] | None:
        return self.fila


class ConexionAsincrona:
    def __init__(self, fila: dict[str, Any] | None, error: Exception | None = None) -> None:
        self.cursor_ = Cursor(fila)
        self.error = error
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))
        if self.error:
            raise self.error

    def cursor(self, row_factory: Any) -> Cursor:
        return self.cursor_


class Pool:
    def __init__(self, conexion: ConexionAsincrona) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[ConexionAsincrona]:
        yield self.conexion


def _fila(definicion: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "2026.10.1",
        "producto_id": "vida-hipotecario",
        "vigente_desde": VIGENTE,
        "definicion": definicion,
        "hash": hash_canonico(definicion),
        "publicado_por": "migracion",
    }


async def test_lecturas_mapean_la_fila(definicion_candidata: dict[str, Any]) -> None:
    conexion = ConexionAsincrona(_fila(definicion_candidata))
    repo = ModelosPg(Pool(conexion))  # type: ignore[arg-type]
    en = datetime(2026, 10, 15, tzinfo=UTC)
    vigente = await repo.vigente("vida-hipotecario", en)
    assert vigente == ModeloPublicado(**_fila(definicion_candidata))
    assert await repo.por_version("2026.10.1") == vigente
    (sql_vigente, p1), (sql_version, p2) = conexion.cursor_.sentencias
    assert "vigente_desde <= %s" in sql_vigente and p1 == ("vida-hipotecario", en)
    assert "WHERE version = %s" in sql_version and p2 == ("2026.10.1",)


async def test_lectura_sin_fila_devuelve_none() -> None:
    repo = ModelosPg(Pool(ConexionAsincrona(None)))  # type: ignore[arg-type]
    assert await repo.por_version("2030.01.1") is None


async def test_guardar_inserta_y_traduce_la_version_repetida(
    definicion_candidata: dict[str, Any],
) -> None:
    modelo = ModeloPublicado(**_fila(definicion_candidata))
    conexion = ConexionAsincrona(None)
    await ModelosPg(Pool(conexion)).guardar(modelo)  # type: ignore[arg-type]
    sql, parametros = conexion.sentencias[0]
    assert sql.startswith("INSERT INTO modelos_riesgo") and "ON CONFLICT" not in sql
    assert parametros[3].obj == definicion_candidata

    repetida = ConexionAsincrona(None, psycopg.errors.UniqueViolation("duplicada"))
    with pytest.raises(VersionExistente):
        await ModelosPg(Pool(repetida)).guardar(modelo)  # type: ignore[arg-type]


def test_reloj_del_sistema_en_utc() -> None:
    assert RelojSistema().ahora().tzinfo is UTC
