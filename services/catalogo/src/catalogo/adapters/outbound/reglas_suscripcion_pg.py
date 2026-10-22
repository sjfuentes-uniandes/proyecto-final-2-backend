"""Reglas de suscripción en PostgreSQL (HU-W13). catalogo es el único escritor de su base.

`sembrar` publica las versiones listadas en `reglasSuscripcion` de cada producto de
`semillas/productos.json`, con el mismo mecanismo que las reglas de prima (HU-W03): el esquema del
borde, la validación del dominio y el hash canónico. Una semilla inválida impide arrancar. Es
idempotente: una versión existente no cambia.
"""

import json
from datetime import datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from catalogo.adapters.inbound.http.esquema_suscripcion import DefinicionReglasSuscripcion, a_dict
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.suscripcion.errores import ReglasSuscripcionInvalidas
from catalogo.domain.suscripcion.reglas import ReglasSuscripcion
from catalogo.domain.suscripcion.validacion import validar_reglas_suscripcion

_SEMILLAS = resources.files("catalogo.adapters.outbound").joinpath("semillas")

_COLUMNAS = "producto_id, version, vigente_desde, definicion, hash, publicado_por"
_INSERTAR = (
    f"INSERT INTO reglas_suscripcion ({_COLUMNAS}) VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (producto_id, version) DO NOTHING"
)
_VIGENTES = (
    f"SELECT {_COLUMNAS} FROM reglas_suscripcion WHERE producto_id = %s AND vigente_desde <= %s "
    "ORDER BY vigente_desde DESC, publicado_en DESC LIMIT 1"
)
_POR_VERSION = f"SELECT {_COLUMNAS} FROM reglas_suscripcion WHERE producto_id = %s AND version = %s"

Semilla = tuple[str, str, datetime, dict[str, Any]]


def semillas() -> list[Semilla]:
    """(producto, versión, vigente desde, definición) de cada versión sembrada.

    Lanza ReglasSuscripcionInvalidas si una definición no cumple sus validaciones.
    """
    productos: list[dict[str, Any]] = json.loads(
        _SEMILLAS.joinpath("productos.json").read_text(encoding="utf-8")
    )
    resultado: list[Semilla] = []
    for producto in productos:
        for entrada in producto.get("reglasSuscripcion", []):
            texto = _SEMILLAS.joinpath(entrada["archivo"]).read_text(encoding="utf-8")
            definicion = a_dict(DefinicionReglasSuscripcion.model_validate_json(texto))
            errores = validar_reglas_suscripcion(definicion, producto["id"])
            if errores:
                raise ReglasSuscripcionInvalidas(errores)
            vigente_desde = datetime.fromisoformat(entrada["vigenteDesde"])
            resultado.append((producto["id"], entrada["version"], vigente_desde, definicion))
    return resultado


def sembrar(conexion: psycopg.Connection[Any]) -> None:
    """Inserta las versiones de la semilla que aún no existen (después de los productos)."""
    for producto_id, version, vigente_desde, definicion in semillas():
        conexion.execute(
            _INSERTAR,
            (
                producto_id,
                version,
                vigente_desde,
                Jsonb(definicion),
                hash_canonico(definicion),
                "migracion",
            ),
        )


def _reglas(fila: dict[str, Any]) -> ReglasSuscripcion:
    return ReglasSuscripcion(
        producto_id=fila["producto_id"],
        version=fila["version"],
        vigente_desde=fila["vigente_desde"],
        definicion=fila["definicion"],
        hash=fila["hash"],
        publicado_por=fila["publicado_por"],
    )


class ReglasSuscripcionPg:
    """Implementa RepositorioReglasSuscripcion. Toma una conexión del pool solo por consulta."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def vigentes(self, producto_id: str, en: datetime) -> ReglasSuscripcion | None:
        return await self._una(_VIGENTES, (producto_id, en))

    async def por_version(self, producto_id: str, version: str) -> ReglasSuscripcion | None:
        return await self._una(_POR_VERSION, (producto_id, version))

    async def _una(self, sql: str, parametros: tuple[Any, ...]) -> ReglasSuscripcion | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            fila = await cursor.fetchone()
        return _reglas(fila) if fila else None
