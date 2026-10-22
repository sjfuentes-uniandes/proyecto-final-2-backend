"""Productos y reglas de prima en PostgreSQL (HU-W03). catalogo es el único escritor de su base.

`sembrar` publica los productos de `semillas/productos.json` y sus reglas, con la misma validación
y el mismo hash canónico que los modelos de riesgo: una semilla inválida impide arrancar. Es
idempotente: un producto existente no cambia y una versión de reglas existente tampoco.
"""

import json
from datetime import datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from catalogo.adapters.inbound.http.esquema_prima import DefinicionReglasPrima, a_dict
from catalogo.domain.productos.errores import ReglasInvalidas
from catalogo.domain.productos.producto import Cobertura, EstadoProducto, Producto, ReglasPrima
from catalogo.domain.productos.validacion import validar_reglas
from catalogo.domain.reglas.hash import hash_canonico

_SEMILLAS = resources.files("catalogo.adapters.outbound").joinpath("semillas")

_COLUMNAS_PRODUCTO = (
    "id, version, nombre, ramo, pais, moneda, estado, vigente_desde, vigente_hasta, coberturas"
)
_COLUMNAS_REGLAS = "producto_id, version, vigente_desde, definicion, hash, publicado_por"
_INSERTAR_PRODUCTO = (
    f"INSERT INTO productos ({_COLUMNAS_PRODUCTO}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (id) DO NOTHING"
)
_INSERTAR_REGLAS = (
    f"INSERT INTO reglas_prima ({_COLUMNAS_REGLAS}) VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (producto_id, version) DO NOTHING"
)
_PRODUCTO = f"SELECT {_COLUMNAS_PRODUCTO} FROM productos WHERE id = %s"
_REGLAS_VIGENTES = (
    f"SELECT {_COLUMNAS_REGLAS} FROM reglas_prima WHERE producto_id = %s AND vigente_desde <= %s "
    "ORDER BY vigente_desde DESC, publicado_en DESC LIMIT 1"
)
_REGLAS_POR_VERSION = (
    f"SELECT {_COLUMNAS_REGLAS} FROM reglas_prima WHERE producto_id = %s AND version = %s"
)


def producto_de(datos: dict[str, Any]) -> Producto:
    """Producto desde su forma JSON (la de la semilla y la de la API)."""
    hasta = datos["vigenteHasta"]
    return Producto(
        id=datos["id"],
        version=datos["version"],
        nombre=datos["nombre"],
        ramo=datos["ramo"],
        pais=datos["pais"],
        moneda=datos["moneda"],
        estado=EstadoProducto(datos["estado"]),
        vigente_desde=datetime.fromisoformat(datos["vigenteDesde"]),
        vigente_hasta=datetime.fromisoformat(hasta) if hasta else None,
        coberturas=tuple(
            Cobertura(c["codigo"], c["nombre"], c["obligatoria"]) for c in datos["coberturas"]
        ),
    )


def semillas() -> list[tuple[Producto, list[tuple[str, datetime, dict[str, Any]]]]]:
    """Cada producto de la semilla con sus reglas (versión, vigente desde, definición).

    Lanza ReglasInvalidas si una definición no cumple el esquema o sus validaciones.
    """
    datos: list[dict[str, Any]] = json.loads(
        _SEMILLAS.joinpath("productos.json").read_text(encoding="utf-8")
    )
    resultado = []
    for entrada in datos:
        producto = producto_de(entrada)
        reglas = []
        for regla in entrada["reglas"]:
            texto = _SEMILLAS.joinpath(regla["archivo"]).read_text(encoding="utf-8")
            definicion = a_dict(DefinicionReglasPrima.model_validate_json(texto))
            errores = validar_reglas(definicion, producto)
            if errores:
                raise ReglasInvalidas(errores)
            vigente_desde = datetime.fromisoformat(regla["vigenteDesde"])
            reglas.append((regla["version"], vigente_desde, definicion))
        resultado.append((producto, reglas))
    return resultado


def sembrar(conexion: psycopg.Connection[Any]) -> None:
    """Inserta los productos y las reglas de la semilla que aún no existen."""
    for producto, reglas in semillas():
        conexion.execute(
            _INSERTAR_PRODUCTO,
            (
                producto.id,
                producto.version,
                producto.nombre,
                producto.ramo,
                producto.pais,
                producto.moneda,
                producto.estado.value,
                producto.vigente_desde,
                producto.vigente_hasta,
                Jsonb([_cobertura_json(c) for c in producto.coberturas]),
            ),
        )
        for version, vigente_desde, definicion in reglas:
            conexion.execute(
                _INSERTAR_REGLAS,
                (
                    producto.id,
                    version,
                    vigente_desde,
                    Jsonb(definicion),
                    hash_canonico(definicion),
                    "migracion",
                ),
            )


def _cobertura_json(cobertura: Cobertura) -> dict[str, Any]:
    return {
        "codigo": cobertura.codigo,
        "nombre": cobertura.nombre,
        "obligatoria": cobertura.obligatoria,
    }


def _producto(fila: dict[str, Any]) -> Producto:
    return Producto(
        id=fila["id"],
        version=fila["version"],
        nombre=fila["nombre"],
        ramo=fila["ramo"],
        pais=fila["pais"],
        moneda=fila["moneda"],
        estado=EstadoProducto(fila["estado"]),
        vigente_desde=fila["vigente_desde"],
        vigente_hasta=fila["vigente_hasta"],
        coberturas=tuple(
            Cobertura(c["codigo"], c["nombre"], c["obligatoria"]) for c in fila["coberturas"]
        ),
    )


def _reglas(fila: dict[str, Any]) -> ReglasPrima:
    return ReglasPrima(
        producto_id=fila["producto_id"],
        version=fila["version"],
        vigente_desde=fila["vigente_desde"],
        definicion=fila["definicion"],
        hash=fila["hash"],
        publicado_por=fila["publicado_por"],
    )


class ProductosPg:
    """Implementa RepositorioProductos. Toma una conexión del pool solo durante cada consulta."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def producto(self, producto_id: str) -> Producto | None:
        fila = await self._uno(_PRODUCTO, (producto_id,))
        return _producto(fila) if fila else None

    async def reglas_vigentes(self, producto_id: str, en: datetime) -> ReglasPrima | None:
        fila = await self._uno(_REGLAS_VIGENTES, (producto_id, en))
        return _reglas(fila) if fila else None

    async def reglas_por_version(self, producto_id: str, version: str) -> ReglasPrima | None:
        fila = await self._uno(_REGLAS_POR_VERSION, (producto_id, version))
        return _reglas(fila) if fila else None

    async def _uno(self, sql: str, parametros: tuple[Any, ...]) -> dict[str, Any] | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            return await cursor.fetchone()
