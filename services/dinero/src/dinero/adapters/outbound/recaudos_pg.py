"""RecaudosRepositorio en PostgreSQL (HU-W16, 16.9). dinero es el único escritor de su base.

- `crear_pendiente`: `INSERT … ON CONFLICT (clave_recaudo) DO NOTHING RETURNING`. Dos entregas a la
  vez con la misma `claveRecaudo`: el `INSERT` de la segunda espera al commit de la primera y no
  inserta nada; luego relee la ganadora (plan §d, concurrencia). Solo la que insertó sale con
  `nuevo = True`, y solo esa cobra (16.10).
- `registrar_resultado`: `UPDATE … WHERE estado = 'PENDIENTE'`. Si otra tarea ya lo resolvió, no
  cambia nada (0 filas) y no choca con el disparador de la migración.
- Toda operación acepta la transacción de otro (`tx`, con su `conexion`: la `TransaccionInboxPg` de
  `solventa_common.inbox`), para que el movimiento y la fila del inbox se confirmen juntos. Sin
  `tx`, toma una conexión del pool con su propia transacción, solo mientras dura la operación.
- `transaccion()` (16.10) abre una transacción para varias operaciones: la continuación del cobro
  escribe en ella el resultado y el evento del outbox, juntos.
- `pendientes` (16.11) lee los PENDIENTE más antiguos para la conciliación, sin bloquearlos: la
  conexión se devuelve antes de llamar al adaptador. Sin índice propio (no hay migraciones nuevas
  en este paso): la tabla es pequeña y el lote, acotado.
- psycopg devuelve `numeric` como `Decimal`: el monto no pasa por `float`.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from dinero.application.recaudo.puertos import Creacion
from dinero.domain.recaudo.modelos import EstadoRecaudo, Moneda, Recaudo

_PAQUETE = resources.files("dinero.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('dinero.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('dinero.migraciones'))"

COLUMNAS = (
    "recaudo_id",
    "clave_recaudo",
    "cotizacion_id",
    "pagador_ref",
    "monto",
    "moneda",
    "periodicidad",
    "estado",
    "transaccion_id",
    "motivo",
    "ultimo_incierto",
    "creado_en",
    "actualizado_en",
    "resuelto_en",
    "correlation_id",
)
# Lo único que cambia una transición; el disparador rechaza tocar lo demás.
_RESULTADO = (
    "estado",
    "transaccion_id",
    "motivo",
    "ultimo_incierto",
    "actualizado_en",
    "resuelto_en",
)
_INSERTAR = (
    f"INSERT INTO recaudos ({', '.join(COLUMNAS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) "
    "ON CONFLICT (clave_recaudo) DO NOTHING RETURNING recaudo_id"
)
_POR_CLAVE = f"SELECT {', '.join(COLUMNAS)} FROM recaudos WHERE clave_recaudo = %s"
_REGISTRAR = (
    f"UPDATE recaudos SET {', '.join(f'{c} = %s' for c in _RESULTADO)} "
    f"WHERE recaudo_id = %s AND estado = '{EstadoRecaudo.PENDIENTE.value}'"
)
# Sin FOR UPDATE: la conciliación no retiene filas mientras llama al adaptador. El desempate por
# recaudo_id da un orden estable entre tareas.
_PENDIENTES = (
    f"SELECT {', '.join(COLUMNAS)} FROM recaudos "
    f"WHERE estado = '{EstadoRecaudo.PENDIENTE.value}' AND actualizado_en < %s "
    "ORDER BY actualizado_en, recaudo_id LIMIT %s"
)


def migrar(url: str) -> None:
    """Aplica los .sql en orden. Idempotente y serializado entre réplicas."""
    archivos = sorted(
        (f for f in _PAQUETE.joinpath("migraciones").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    with psycopg.connect(url, autocommit=True) as conexion:
        conexion.execute(_LOCK)
        try:
            for archivo in archivos:
                conexion.execute(archivo.read_text(encoding="utf-8"))
        finally:
            conexion.execute(_UNLOCK)


def fila_recaudo(recaudo: Recaudo) -> dict[str, Any]:
    return {
        "recaudo_id": recaudo.recaudo_id,
        "clave_recaudo": recaudo.clave_recaudo,
        "cotizacion_id": recaudo.cotizacion_id,
        "pagador_ref": recaudo.pagador_ref,
        "monto": recaudo.monto,
        "moneda": recaudo.moneda.value,
        "periodicidad": recaudo.periodicidad,
        "estado": recaudo.estado.value,
        "transaccion_id": recaudo.transaccion_id,
        "motivo": recaudo.motivo,
        "ultimo_incierto": recaudo.ultimo_incierto,
        "creado_en": recaudo.creado_en,
        "actualizado_en": recaudo.actualizado_en,
        "resuelto_en": recaudo.resuelto_en,
        "correlation_id": recaudo.correlation_id,
    }


def recaudo_de(fila: Mapping[str, Any]) -> Recaudo:
    datos = {c: fila[c] for c in COLUMNAS}
    datos["moneda"] = Moneda(fila["moneda"])
    datos["estado"] = EstadoRecaudo(fila["estado"])
    return Recaudo(**datos)


class ConTransaccion(Protocol):
    """La transacción de otro, como `solventa_common.inbox.TransaccionInboxPg`."""

    @property
    def conexion(self) -> AsyncConnection[Any]: ...


@dataclass(frozen=True)
class TransaccionPg:
    """Una transacción propia de dinero, con la misma forma que la del inbox."""

    conexion: AsyncConnection[Any]


class RecaudosPg:
    """Implementa RecaudosRepositorio, PendientesRepositorio y Transacciones, con
    ConTransaccion."""

    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    @asynccontextmanager
    async def _conexion(self, tx: ConTransaccion | None) -> AsyncIterator[AsyncConnection[Any]]:
        if tx is not None:
            # La transacción la abre y la confirma (o revierte) quien la pasa.
            yield tx.conexion
            return
        async with self._pool.connection() as conexion, conexion.transaction():
            yield conexion

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[ConTransaccion]:
        async with self._pool.connection() as conexion, conexion.transaction():
            yield TransaccionPg(conexion)

    async def crear_pendiente(self, recaudo: Recaudo, tx: ConTransaccion | None = None) -> Creacion:
        if recaudo.estado is not EstadoRecaudo.PENDIENTE:
            raise ValueError("crear_pendiente: el movimiento debe estar PENDIENTE")
        fila = fila_recaudo(recaudo)
        async with self._conexion(tx) as conexion:
            cursor = await conexion.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
            if await cursor.fetchone() is not None:
                return Creacion(recaudo, nuevo=True)
            existente = await self._por_clave(conexion, recaudo.clave_recaudo)
        if existente is None:
            # El disparador impide borrar: un conflicto sin fila visible no debería ocurrir.
            raise RuntimeError("crear_pendiente: conflicto por clave_recaudo sin fila existente")
        return Creacion(existente, nuevo=False)

    async def por_clave(
        self, clave_recaudo: UUID, tx: ConTransaccion | None = None
    ) -> Recaudo | None:
        async with self._conexion(tx) as conexion:
            return await self._por_clave(conexion, clave_recaudo)

    async def registrar_resultado(self, recaudo: Recaudo, tx: ConTransaccion | None = None) -> int:
        fila = fila_recaudo(recaudo)
        async with self._conexion(tx) as conexion:
            cursor = await conexion.execute(
                _REGISTRAR, (*(fila[c] for c in _RESULTADO), recaudo.recaudo_id)
            )
            return cursor.rowcount

    async def pendientes(self, antes_de: datetime, lote: int) -> list[Recaudo]:
        async with self._conexion(None) as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(_PENDIENTES, (antes_de, lote))
            return [recaudo_de(fila) for fila in await cursor.fetchall()]

    @staticmethod
    async def _por_clave(conexion: AsyncConnection[Any], clave_recaudo: UUID) -> Recaudo | None:
        cursor = conexion.cursor(row_factory=dict_row)
        await cursor.execute(_POR_CLAVE, (clave_recaudo,))
        fila = await cursor.fetchone()
        return None if fila is None else recaudo_de(fila)
