"""ProductosPg y la siembra de productos con conexiones falsas (sin base)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from catalogo.adapters.outbound.productos_pg import ProductosPg, sembrar, semillas
from catalogo.domain.productos.producto import EstadoProducto
from catalogo.domain.reglas.hash import hash_canonico

pytestmark = pytest.mark.anyio
VIGENTE = datetime(2026, 9, 1, tzinfo=UTC)


class Cursor:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.fila = fila
        self.sentencias: list[tuple[str, Any]] = []

    async def execute(self, sql: str, parametros: Any) -> None:
        self.sentencias.append((sql, parametros))

    async def fetchone(self) -> dict[str, Any] | None:
        return self.fila


class Conexion:
    def __init__(self, fila: dict[str, Any] | None) -> None:
        self.cursor_ = Cursor(fila)

    def cursor(self, row_factory: Any) -> Cursor:
        return self.cursor_


class Pool:
    def __init__(self, conexion: Conexion) -> None:
        self.conexion = conexion

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conexion]:
        yield self.conexion


class ConexionSincrona:
    def __init__(self) -> None:
        self.sentencias: list[tuple[str, Any]] = []

    def execute(self, sql: str, parametros: Any = None) -> None:
        self.sentencias.append((sql, parametros))


def test_sembrar_inserta_productos_y_reglas_sin_pisar_los_existentes() -> None:
    conexion = ConexionSincrona()
    sembrar(conexion)  # type: ignore[arg-type]
    productos = [(s, p) for s, p in conexion.sentencias if "INSERT INTO productos" in s]
    reglas = [(s, p) for s, p in conexion.sentencias if "INSERT INTO reglas_prima" in s]
    assert [p[0] for _, p in productos] == ["vida-hipotecario", "vida-hipotecario-2025"]
    assert all("ON CONFLICT (id) DO NOTHING" in s for s, _ in productos)
    assert all("ON CONFLICT (producto_id, version) DO NOTHING" in s for s, _ in reglas)
    producto, version, desde, definicion, hash_, actor = reglas[0][1]
    assert (producto, version, desde, actor) == (
        "vida-hipotecario",
        "2026.09.1",
        VIGENTE,
        "migracion",
    )
    assert hash_ == hash_canonico(definicion.obj)
    coberturas = productos[0][1][9].obj
    assert coberturas[2] == {
        "codigo": "ENFERMEDADES_GRAVES",
        "nombre": "Enfermedades graves",
        "obligatoria": False,
    }


def _fila_producto() -> dict[str, Any]:
    producto = semillas()[1][0]
    return {
        "id": producto.id,
        "version": producto.version,
        "nombre": producto.nombre,
        "ramo": producto.ramo,
        "pais": producto.pais,
        "moneda": producto.moneda,
        "estado": "INACTIVO",
        "vigente_desde": producto.vigente_desde,
        "vigente_hasta": producto.vigente_hasta,
        "coberturas": [
            {"codigo": c.codigo, "nombre": c.nombre, "obligatoria": c.obligatoria}
            for c in producto.coberturas
        ],
    }


async def test_producto_mapea_la_fila() -> None:
    conexion = Conexion(_fila_producto())
    producto = await ProductosPg(Pool(conexion)).producto("vida-hipotecario-2025")  # type: ignore[arg-type]
    assert producto is not None
    assert producto.estado is EstadoProducto.INACTIVO
    assert producto.vigente_hasta == datetime(2026, 9, 1, tzinfo=UTC)
    assert conexion.cursor_.sentencias[0][1] == ("vida-hipotecario-2025",)


async def test_reglas_mapean_la_fila_y_sin_fila_es_none() -> None:
    _, reglas = semillas()[0]
    version, desde, definicion = reglas[0]
    fila = {
        "producto_id": "vida-hipotecario",
        "version": version,
        "vigente_desde": desde,
        "definicion": definicion,
        "hash": hash_canonico(definicion),
        "publicado_por": "migracion",
    }
    conexion = Conexion(fila)
    repo = ProductosPg(Pool(conexion))  # type: ignore[arg-type]
    en = datetime(2026, 10, 15, tzinfo=UTC)
    vigentes = await repo.reglas_vigentes("vida-hipotecario", en)
    assert vigentes is not None and vigentes.version == "2026.09.1"
    assert await repo.reglas_por_version("vida-hipotecario", "2026.09.1") == vigentes
    (sql_vigente, p1), (sql_version, p2) = conexion.cursor_.sentencias
    assert "vigente_desde <= %s" in sql_vigente and p1 == ("vida-hipotecario", en)
    assert "version = %s" in sql_version and p2 == ("vida-hipotecario", "2026.09.1")

    vacio = ProductosPg(Pool(Conexion(None)))  # type: ignore[arg-type]
    assert await vacio.producto("x") is None
    assert await vacio.reglas_vigentes("x", en) is None
    assert await vacio.reglas_por_version("x", "2026.09.1") is None
