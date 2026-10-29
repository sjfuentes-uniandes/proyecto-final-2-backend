"""Reglas de fallback en PostgreSQL (HU-W06). catalogo es el único escritor de su base.

`sembrar` publica las versiones listadas en `reglasFallback` de cada producto de
`semillas/productos.json`, con el mismo mecanismo que las reglas de suscripción (HU-W13): el esquema
del borde, la validación del dominio (contra el modelo de riesgo del producto) y el hash canónico.
Una semilla inválida impide arrancar. Es idempotente: una versión existente no cambia.
"""

import json
from collections.abc import Mapping
from datetime import datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from catalogo.adapters.inbound.http.esquema_fallback import DefinicionReglasFallback, a_dict
from catalogo.domain.fallback.errores import ReglasFallbackInvalidas
from catalogo.domain.fallback.reglas import ReglasFallback
from catalogo.domain.fallback.validacion import validar_reglas_fallback
from catalogo.domain.reglas.hash import hash_canonico

_SEMILLAS = resources.files("catalogo.adapters.outbound").joinpath("semillas")

_COLUMNAS = "producto_id, version, vigente_desde, vigente_hasta, definicion, hash, publicado_por"
_INSERTAR = (
    f"INSERT INTO reglas_fallback ({_COLUMNAS}) VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (producto_id, version) DO NOTHING"
)
# La de mayor vigente_desde ≤ en; si esa venció, no hay reglas vigentes (no se vuelve a una
# anterior: la versión nueva reemplazó a la vieja).
_VIGENTES = (
    f"SELECT {_COLUMNAS} FROM (SELECT {_COLUMNAS}, publicado_en FROM reglas_fallback "
    "WHERE producto_id = %s AND vigente_desde <= %s "
    "ORDER BY vigente_desde DESC, publicado_en DESC LIMIT 1) ultima "
    "WHERE vigente_hasta IS NULL OR %s < vigente_hasta"
)
_POR_VERSION = f"SELECT {_COLUMNAS} FROM reglas_fallback WHERE producto_id = %s AND version = %s"

Semilla = tuple[str, str, datetime, datetime | None, dict[str, Any]]


def semillas(modelos: Mapping[str, Mapping[str, Any]]) -> list[Semilla]:
    """(producto, versión, vigente desde, vigente hasta, definición) de cada versión sembrada.

    `modelos` es el modelo de riesgo de cada producto, contra el que se valida la obligatoriedad.
    Lanza ReglasFallbackInvalidas si una definición no cumple sus validaciones.
    """
    productos: list[dict[str, Any]] = json.loads(
        _SEMILLAS.joinpath("productos.json").read_text(encoding="utf-8")
    )
    resultado: list[Semilla] = []
    for producto in productos:
        for entrada in producto.get("reglasFallback", []):
            modelo = modelos.get(producto["id"])
            if modelo is None:
                raise ReglasFallbackInvalidas(("SIN_MODELO_DE_RIESGO",))
            texto = _SEMILLAS.joinpath(entrada["archivo"]).read_text(encoding="utf-8")
            definicion = a_dict(DefinicionReglasFallback.model_validate_json(texto))
            vigente_desde = datetime.fromisoformat(entrada["vigenteDesde"])
            hasta = entrada.get("vigenteHasta")
            vigente_hasta = datetime.fromisoformat(hasta) if hasta else None
            errores = validar_reglas_fallback(
                definicion, producto["id"], modelo, vigente_desde, vigente_hasta
            )
            if errores:
                raise ReglasFallbackInvalidas(errores)
            resultado.append(
                (producto["id"], entrada["version"], vigente_desde, vigente_hasta, definicion)
            )
    return resultado


def sembrar(conexion: psycopg.Connection[Any], modelos: Mapping[str, Mapping[str, Any]]) -> None:
    """Inserta las versiones de la semilla que aún no existen (después de los productos)."""
    for producto_id, version, vigente_desde, vigente_hasta, definicion in semillas(modelos):
        conexion.execute(
            _INSERTAR,
            (
                producto_id,
                version,
                vigente_desde,
                vigente_hasta,
                Jsonb(definicion),
                hash_canonico(definicion),
                "migracion",
            ),
        )


def _reglas(fila: dict[str, Any]) -> ReglasFallback:
    return ReglasFallback(
        producto_id=fila["producto_id"],
        version=fila["version"],
        vigente_desde=fila["vigente_desde"],
        vigente_hasta=fila["vigente_hasta"],
        definicion=fila["definicion"],
        hash=fila["hash"],
        publicado_por=fila["publicado_por"],
    )


class ReglasFallbackPg:
    """Implementa RepositorioReglasFallback. Toma una conexión del pool solo por consulta."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def vigentes(self, producto_id: str, en: datetime) -> ReglasFallback | None:
        return await self._una(_VIGENTES, (producto_id, en, en))

    async def por_version(self, producto_id: str, version: str) -> ReglasFallback | None:
        return await self._una(_POR_VERSION, (producto_id, version))

    async def _una(self, sql: str, parametros: tuple[Any, ...]) -> ReglasFallback | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            fila = await cursor.fetchone()
        return _reglas(fila) if fila else None
