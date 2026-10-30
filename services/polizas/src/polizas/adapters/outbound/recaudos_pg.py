"""Proyección del recaudo en PostgreSQL (HU-W16, 16.13): tabla `recaudos_aceptacion` de 003.

`guardar` escribe y lee en la transacción del inbox (`TransaccionInboxPg.conexion`); `por_clave`
lee con el pool. Si la aceptación es conocida lo dice `PolizasPg.origen` (HU-W17, 17.6). `ON CONFLICT (clave_recaudo) DO
NOTHING` deja el primer estado final: con dos entregas concurrentes de la misma clave, el segundo
`INSERT` espera al commit del primero y luego lee su fila. Las migraciones las aplica
`decisiones_pg.migrar` (todos los .sql en orden).
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from solventa_common.inbox import TransaccionInboxPg

from polizas.domain.recaudo.modelos import ResultadoRecaudo, SituacionRecaudo

COLUMNAS = (
    "clave_recaudo",
    "recaudo_id",
    "estado",
    "monto",
    "moneda",
    "transaccion_id",
    "motivo",
    "ocurrido_en",
)
_INSERTAR = (
    f"INSERT INTO recaudos_aceptacion ({', '.join(COLUMNAS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNAS))}) "
    "ON CONFLICT (clave_recaudo) DO NOTHING RETURNING clave_recaudo"
)
_POR_CLAVE = f"SELECT {', '.join(COLUMNAS)} FROM recaudos_aceptacion WHERE clave_recaudo = %s"


def fila_recaudo(resultado: ResultadoRecaudo) -> dict[str, Any]:
    """El monto va como Decimal: psycopg lo envía como numeric, sin pasar por float."""
    return {
        "clave_recaudo": resultado.clave_recaudo,
        "recaudo_id": resultado.recaudo_id,
        "estado": resultado.estado.value,
        "monto": resultado.monto,
        "moneda": resultado.moneda,
        "transaccion_id": resultado.transaccion_id,
        "motivo": resultado.motivo,
        "ocurrido_en": resultado.ocurrido_en,
    }


def recaudo_de(fila: Mapping[str, Any]) -> ResultadoRecaudo:
    return ResultadoRecaudo(
        clave_recaudo=fila["clave_recaudo"],
        recaudo_id=fila["recaudo_id"],
        estado=SituacionRecaudo(fila["estado"]),
        monto=fila["monto"],
        moneda=fila["moneda"],
        transaccion_id=fila["transaccion_id"],
        motivo=fila["motivo"],
        ocurrido_en=fila["ocurrido_en"],
    )


class RecaudosPg:
    """Implementa RecaudosRepositorio y ProyeccionRecaudos[TransaccionInboxPg]."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def por_clave(self, clave_recaudo: UUID) -> ResultadoRecaudo | None:
        async with self._pool.connection() as conexion:
            cursor = conexion.cursor(row_factory=dict_row)
            await cursor.execute(_POR_CLAVE, (clave_recaudo,))
            fila = await cursor.fetchone()
        return None if fila is None else recaudo_de(fila)

    async def guardar(
        self, resultado: ResultadoRecaudo, tx: TransaccionInboxPg
    ) -> ResultadoRecaudo | None:
        fila = fila_recaudo(resultado)
        cursor = await tx.conexion.execute(_INSERTAR, [fila[c] for c in COLUMNAS])
        if await cursor.fetchone() is not None:
            return None
        lectura = tx.conexion.cursor(row_factory=dict_row)
        await lectura.execute(_POR_CLAVE, (resultado.clave_recaudo,))
        previa = await lectura.fetchone()
        if previa is None:
            # No pasa: el conflicto implica una fila confirmada. Si pasara, rollback y reentrega.
            raise RuntimeError("recaudo: conflicto sin fila")
        return recaudo_de(previa)
