"""Repositorio de consentimientos sobre Postgres (ADR 0009 y 0010).

Toda marca de tiempo sale de una única captura de `clock_timestamp()` por sentencia
(`WITH t AS MATERIALIZED`): nunca `now()` ni el reloj de la aplicación.
"""

from datetime import UTC, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from clientes.adapters.outbound import outbox_pg
from clientes.application.consentimientos.puertos import Otorgamiento
from clientes.domain.consentimientos.modelos import (
    ClaveIdempotenciaEnUso,
    Consentimiento,
    ConsentimientoActivoExistente,
    EstadoConsentimiento,
    RegistroIdempotencia,
)

RETENCION_CLAVES = timedelta(hours=24)
_INDICE_ACTIVO = "consentimiento_activo_unico"
_RESTRICCION_CLAVE = "consentimiento_idempotencia_pk"

_RELOJ = "WITH t AS MATERIALIZED (SELECT clock_timestamp() AS ahora) "
_COLUMNAS = (
    "c.id, c.cliente_id, c.proposito, c.alcances, c.version_texto, c.idioma, c.estado, "
    "c.otorgado_en, c.vigente_hasta, c.revocado_en"
)
_LEER = _RELOJ + f"SELECT {_COLUMNAS}, t.ahora FROM consentimiento c CROSS JOIN t "
_POR_ID = _LEER + "WHERE c.id = %s AND c.cliente_id = %s"
_ACTIVO = _LEER + "WHERE c.cliente_id = %s AND c.proposito = %s AND c.estado = 'ACTIVO'"
_LISTAR = _LEER + "WHERE c.cliente_id = %s ORDER BY c.otorgado_en DESC"
_POR_CLAVE = (
    _RELOJ + f"SELECT {_COLUMNAS}, t.ahora, i.hash_cuerpo FROM consentimiento_idempotencia i "
    "JOIN consentimiento c ON c.id = i.consentimiento_id CROSS JOIN t "
    "WHERE i.cliente_id = %s AND i.clave = %s AND i.creado_en > t.ahora - %s"
)
_BARRER_CLAVES = (
    _RELOJ + "DELETE FROM consentimiento_idempotencia i USING t WHERE i.creado_en <= t.ahora - %s"
)
# Libera el índice único cuando el ACTIVO almacenado ya venció (ADR 0009).
_MATERIALIZAR_VENCIDO = (
    _RELOJ + "UPDATE consentimiento c SET estado = 'VENCIDO' FROM t "
    "WHERE c.cliente_id = %s AND c.proposito = %s AND c.estado = 'ACTIVO' "
    "AND c.vigente_hasta <= t.ahora"
)
_INSERTAR = (
    _RELOJ + "INSERT INTO consentimiento (id, cliente_id, proposito, alcances, version_texto, "
    "idioma, estado, otorgado_en, vigente_hasta) "
    "SELECT %s, %s, %s, %s, %s, %s, 'ACTIVO', t.ahora, fin_de_vigencia(t.ahora, %s::int) FROM t "
    "RETURNING id, cliente_id, proposito, alcances, version_texto, idioma, estado, otorgado_en, "
    "vigente_hasta, revocado_en, otorgado_en AS ahora"
)
_INSERTAR_CLAVE = (
    "INSERT INTO consentimiento_idempotencia "
    "(cliente_id, clave, hash_cuerpo, consentimiento_id, creado_en) VALUES (%s, %s, %s, %s, %s)"
)


def _consentimiento(fila: dict[str, Any]) -> Consentimiento:
    return Consentimiento(
        id=fila["id"],
        cliente_id=fila["cliente_id"],
        proposito=fila["proposito"],
        alcances=tuple(fila["alcances"]),
        version_texto=fila["version_texto"],
        idioma=fila["idioma"],
        estado=EstadoConsentimiento(fila["estado"]),
        otorgado_en=fila["otorgado_en"].astimezone(UTC),
        vigente_hasta=fila["vigente_hasta"].astimezone(UTC),
        revocado_en=fila["revocado_en"].astimezone(UTC) if fila["revocado_en"] else None,
        leido_en=fila["ahora"].astimezone(UTC),
    )


class ConsentimientosPg:
    """Implementa RepositorioConsentimientos. Toma una conexión del pool solo durante cada
    operación."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _filas(self, sql: str, parametros: tuple[Any, ...]) -> list[dict[str, Any]]:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(sql, parametros)
            return await cursor.fetchall()

    async def por_clave(self, cliente_id: UUID, clave: UUID) -> RegistroIdempotencia | None:
        filas = await self._filas(_POR_CLAVE, (cliente_id, clave, RETENCION_CLAVES))
        if not filas:
            return None
        return RegistroIdempotencia(filas[0]["hash_cuerpo"], _consentimiento(filas[0]))

    async def activo(self, cliente_id: UUID, proposito: str) -> Consentimiento | None:
        filas = await self._filas(_ACTIVO, (cliente_id, proposito))
        return _consentimiento(filas[0]) if filas else None

    async def obtener(self, cliente_id: UUID, consentimiento_id: UUID) -> Consentimiento | None:
        filas = await self._filas(_POR_ID, (consentimiento_id, cliente_id))
        return _consentimiento(filas[0]) if filas else None

    async def listar(self, cliente_id: UUID) -> list[Consentimiento]:
        return [_consentimiento(fila) for fila in await self._filas(_LISTAR, (cliente_id,))]

    async def crear(self, otorgamiento: Otorgamiento) -> Consentimiento:
        solicitud = otorgamiento.solicitud
        try:
            async with self._pool.connection() as conexion, conexion.transaction():
                await conexion.execute(_BARRER_CLAVES, (RETENCION_CLAVES,))
                await conexion.execute(
                    _MATERIALIZAR_VENCIDO, (otorgamiento.cliente_id, solicitud.proposito)
                )
                cursor = conexion.cursor(row_factory=dict_row)
                await cursor.execute(
                    _INSERTAR,
                    (
                        otorgamiento.id,
                        otorgamiento.cliente_id,
                        solicitud.proposito,
                        list(solicitud.alcances),
                        solicitud.version_texto,
                        solicitud.idioma,
                        solicitud.vigencia_meses,
                    ),
                )
                fila = await cursor.fetchone()
                assert fila is not None  # INSERT ... RETURNING siempre devuelve la fila creada
                creado = _consentimiento(fila)
                await conexion.execute(
                    _INSERTAR_CLAVE,
                    (
                        otorgamiento.cliente_id,
                        otorgamiento.clave,
                        otorgamiento.huella,
                        creado.id,
                        creado.otorgado_en,
                    ),
                )
                await outbox_pg.insertar(
                    conexion,
                    outbox_pg.consentimiento_otorgado(
                        creado, otorgamiento.actor_id, otorgamiento.correlation_id
                    ),
                )
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name == _INDICE_ACTIVO:
                raise ConsentimientoActivoExistente() from error
            if error.diag.constraint_name == _RESTRICCION_CLAVE:
                raise ClaveIdempotenciaEnUso() from error
            raise
        return creado
