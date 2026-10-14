"""Repositorio de modelos de riesgo en PostgreSQL. catalogo es el único escritor de su base."""

import json
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from catalogo.domain.reglas.errores import VersionExistente
from catalogo.domain.reglas.hash import hash_canonico
from catalogo.domain.reglas.modelo import ModeloPublicado

_PAQUETE = resources.files("catalogo.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('catalogo.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('catalogo.migraciones'))"

# Semilla: el modelo candidato de DT-03 (04 §B.6.1).
SEMILLA_VERSION = "2026.10.1"
SEMILLA_PRODUCTO = "vida-hipotecario"
SEMILLA_VIGENTE_DESDE = datetime(2026, 10, 1, tzinfo=UTC)

_COLUMNAS = "version, producto_id, vigente_desde, definicion, hash, publicado_por"
_INSERTAR = f"INSERT INTO modelos_riesgo ({_COLUMNAS}) VALUES (%s, %s, %s, %s, %s, %s)"
_POR_VERSION = f"SELECT {_COLUMNAS} FROM modelos_riesgo WHERE version = %s"
_VIGENTE = (
    f"SELECT {_COLUMNAS} FROM modelos_riesgo WHERE producto_id = %s AND vigente_desde <= %s "
    "ORDER BY vigente_desde DESC, publicado_en DESC LIMIT 1"
)


def definicion_semilla() -> dict[str, Any]:
    texto = _PAQUETE.joinpath("semillas", "modelo_2026_10_1.json").read_text(encoding="utf-8")
    definicion: dict[str, Any] = json.loads(texto)
    return definicion


def migrar(url: str) -> None:
    """Aplica los .sql en orden y siembra el modelo candidato. Idempotente y serializado."""
    archivos = sorted(
        (f for f in _PAQUETE.joinpath("migraciones").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    definicion = definicion_semilla()
    with psycopg.connect(url, autocommit=True) as conexion:
        # Serializa las migraciones entre réplicas.
        conexion.execute(_LOCK)
        try:
            for archivo in archivos:
                conexion.execute(archivo.read_text(encoding="utf-8"))
            conexion.execute(
                _INSERTAR + " ON CONFLICT (version) DO NOTHING",
                (
                    SEMILLA_VERSION,
                    SEMILLA_PRODUCTO,
                    SEMILLA_VIGENTE_DESDE,
                    Jsonb(definicion),
                    hash_canonico(definicion),
                    "migracion",
                ),
            )
        finally:
            conexion.execute(_UNLOCK)


def _modelo(fila: dict[str, Any]) -> ModeloPublicado:
    return ModeloPublicado(
        version=fila["version"],
        producto_id=fila["producto_id"],
        vigente_desde=fila["vigente_desde"],
        definicion=fila["definicion"],
        hash=fila["hash"],
        publicado_por=fila["publicado_por"],
    )


class ModelosPg:
    """Implementa RepositorioModelos. Toma una conexión del pool solo durante cada consulta."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def guardar(self, modelo: ModeloPublicado) -> None:
        try:
            async with self._pool.connection() as conexion:
                await conexion.execute(
                    _INSERTAR,
                    (
                        modelo.version,
                        modelo.producto_id,
                        modelo.vigente_desde,
                        Jsonb(dict(modelo.definicion)),
                        modelo.hash,
                        modelo.publicado_por,
                    ),
                )
        except psycopg.errors.UniqueViolation as error:
            raise VersionExistente() from error

    async def vigente(self, producto_id: str, en: datetime) -> ModeloPublicado | None:
        return await self._uno(_VIGENTE, (producto_id, en))

    async def por_version(self, version: str) -> ModeloPublicado | None:
        return await self._uno(_POR_VERSION, (version,))

    async def _uno(self, sql: str, parametros: tuple[Any, ...]) -> ModeloPublicado | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            fila = await cursor.fetchone()
        return _modelo(fila) if fila else None
