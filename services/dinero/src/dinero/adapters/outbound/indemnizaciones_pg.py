"""IndemnizacionesRepositorio en PostgreSQL (HU-W23; ADR 0015). dinero es el único escritor de su base.

- `registrar_pendiente`: `INSERT … ON CONFLICT (siniestro_id, tipo) DO UPDATE SET
  duplicados_descartados = duplicados_descartados + 1 RETURNING …, (xmax = 0) AS nuevo`. Dos
  entregas a la vez (otro `eventId` del mismo siniestro, o dos consumidores): el `INSERT` de la
  segunda espera al commit de la primera y, en vez de insertar, suma un duplicado; solo la que
  insertó sale con `nuevo = True`, y solo esa paga. No toca nada más.
- `registrar_resultado`: `UPDATE … WHERE estado = 'PENDIENTE'`. Si otra tarea ya la resolvió, no
  cambia nada (0 filas) y no choca con el disparador de la migración.
- `descartar_duplicado`: suma uno al contador en cualquier estado (el disparador solo lo permite
  crecer).
- `reclamar` (DS3-18): una transacción corta con `FOR UPDATE SKIP LOCKED` que marca
  `reclamado_hasta` y devuelve las filas; la conexión vuelve al pool **antes** de llamar a la
  pasarela.
- Toda operación acepta la transacción de otro (`tx`, con su `conexion`), para que la indemnización
  y la fila del inbox se confirmen juntas. Sin `tx`, toma una conexión del pool con su propia
  transacción, solo mientras dura la operación. Las transacciones propias (`transaccion()`) son las
  de `RecaudosPg`: el pool es el mismo.
- psycopg devuelve `numeric` como `Decimal` con la escala de la columna (4): se normaliza a 2
  decimales, que es lo que admite la pasarela y lo que escribe el dominio.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from dinero.adapters.outbound.recaudos_pg import ConTransaccion, TransaccionPg
from dinero.application.indemnizacion.puertos import Registro
from dinero.domain.indemnizacion.modelos import EstadoIndemnizacion, Indemnizacion, Moneda

COLUMNAS = (
    "indemnizacion_id",
    "siniestro_id",
    "tipo",
    "referencia",
    "monto",
    "moneda",
    "beneficiario_ref",
    "estado",
    "transaccion_id",
    "motivo",
    "intentos",
    "duplicados_descartados",
    "ultimo_incierto",
    "reclamado_hasta",
    "creado_en",
    "actualizado_en",
    "resuelto_en",
    "correlation_id",
)
_LISTA = ", ".join(COLUMNAS)
# Lo único que cambia una transición; el disparador rechaza tocar lo demás.
_RESULTADO = (
    "estado",
    "transaccion_id",
    "motivo",
    "intentos",
    "ultimo_incierto",
    "reclamado_hasta",
    "actualizado_en",
    "resuelto_en",
)
# `xmax = 0` distingue la fila insertada de la actualizada por el conflicto.
_INSERTAR = (
    f"INSERT INTO indemnizaciones ({_LISTA}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) "
    "ON CONFLICT (siniestro_id, tipo) DO UPDATE "
    "SET duplicados_descartados = indemnizaciones.duplicados_descartados + 1 "
    f"RETURNING {_LISTA}, (xmax = 0) AS nuevo"
)
_REGISTRAR = (
    f"UPDATE indemnizaciones SET {', '.join(f'{c} = %s' for c in _RESULTADO)} "
    f"WHERE indemnizacion_id = %s AND estado = '{EstadoIndemnizacion.PENDIENTE.value}'"
)
_DUPLICADO = (
    "UPDATE indemnizaciones SET duplicados_descartados = duplicados_descartados + 1 "
    "WHERE indemnizacion_id = %s"
)
# La regla de `domain.indemnizacion.conciliacion.seleccionar_pendientes`, con SKIP LOCKED: varias
# réplicas no toman la misma fila. El desempate por id da un orden estable.
_RECLAMAR = (
    "WITH lote AS ("
    "  SELECT indemnizacion_id FROM indemnizaciones "
    f"  WHERE estado = '{EstadoIndemnizacion.PENDIENTE.value}' AND actualizado_en < %s "
    "    AND (reclamado_hasta IS NULL OR reclamado_hasta < %s) "
    "  ORDER BY actualizado_en, indemnizacion_id LIMIT %s FOR UPDATE SKIP LOCKED) "
    "UPDATE indemnizaciones i SET reclamado_hasta = %s FROM lote "
    "WHERE i.indemnizacion_id = lote.indemnizacion_id "
    f"RETURNING {', '.join(f'i.{c}' for c in COLUMNAS)}"
)
_POR_SINIESTRO = f"SELECT {_LISTA} FROM indemnizaciones WHERE siniestro_id = %s ORDER BY creado_en"

_DOS_DECIMALES = Decimal("0.01")


def fila_indemnizacion(indemnizacion: Indemnizacion) -> dict[str, Any]:
    return {
        "indemnizacion_id": indemnizacion.indemnizacion_id,
        "siniestro_id": indemnizacion.siniestro_id,
        "tipo": indemnizacion.tipo,
        "referencia": indemnizacion.referencia,
        "monto": indemnizacion.monto,
        "moneda": indemnizacion.moneda.value,
        "beneficiario_ref": indemnizacion.beneficiario_ref,
        "estado": indemnizacion.estado.value,
        "transaccion_id": indemnizacion.transaccion_id,
        "motivo": indemnizacion.motivo,
        "intentos": indemnizacion.intentos,
        "duplicados_descartados": indemnizacion.duplicados_descartados,
        "ultimo_incierto": indemnizacion.ultimo_incierto,
        "reclamado_hasta": indemnizacion.reclamado_hasta,
        "creado_en": indemnizacion.creado_en,
        "actualizado_en": indemnizacion.actualizado_en,
        "resuelto_en": indemnizacion.resuelto_en,
        "correlation_id": indemnizacion.correlation_id,
    }


def indemnizacion_de(fila: Mapping[str, Any]) -> Indemnizacion:
    datos = {c: fila[c] for c in COLUMNAS}
    datos["monto"] = fila["monto"].quantize(_DOS_DECIMALES)
    datos["moneda"] = Moneda(fila["moneda"])
    datos["estado"] = EstadoIndemnizacion(fila["estado"])
    return Indemnizacion(**datos)


class IndemnizacionesPg:
    """Implementa IndemnizacionesRepositorio, IndemnizacionesLectura y Transacciones, con
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

    async def registrar_pendiente(
        self, indemnizacion: Indemnizacion, tx: ConTransaccion | None = None
    ) -> Registro:
        fila = fila_indemnizacion(indemnizacion)
        async with self._conexion(tx) as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
            guardada = await cursor.fetchone()
        if guardada is None:  # pragma: no cover - RETURNING siempre devuelve la fila
            raise RuntimeError("registrar_pendiente: el INSERT no devolvió la fila")
        return Registro(indemnizacion_de(guardada), nuevo=bool(guardada["nuevo"]))

    async def registrar_resultado(
        self, indemnizacion: Indemnizacion, tx: ConTransaccion | None = None
    ) -> int:
        fila = fila_indemnizacion(indemnizacion)
        async with self._conexion(tx) as conexion:
            cursor = await conexion.execute(
                _REGISTRAR, (*(fila[c] for c in _RESULTADO), indemnizacion.indemnizacion_id)
            )
            return cursor.rowcount

    async def descartar_duplicado(
        self, indemnizacion_id: UUID, tx: ConTransaccion | None = None
    ) -> None:
        async with self._conexion(tx) as conexion:
            await conexion.execute(_DUPLICADO, (indemnizacion_id,))

    async def reclamar(
        self, antes_de: datetime, ahora: datetime, hasta: datetime, lote: int
    ) -> list[Indemnizacion]:
        async with self._conexion(None) as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(_RECLAMAR, (antes_de, ahora, lote, hasta))
            filas = await cursor.fetchall()
        reclamadas = [indemnizacion_de(f) for f in filas]
        return sorted(reclamadas, key=lambda i: (i.actualizado_en, str(i.indemnizacion_id)))

    async def por_siniestro(self, siniestro_id: UUID) -> list[Indemnizacion]:
        async with self._conexion(None) as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(_POR_SINIESTRO, (siniestro_id,))
            return [indemnizacion_de(fila) for fila in await cursor.fetchall()]
