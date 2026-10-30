"""LecturaPolizas en PostgreSQL (HU-M10): lee la tabla `poliza_cliente`."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.rows import tuple_row

from consulta.adapters.outbound.polizas_pg import COLUMNAS
from consulta.domain.polizas.modelos import CoberturaProyectada, PolizaProyectada, Vigencia

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_SELECT = f"SELECT {', '.join(COLUMNAS)} FROM poliza_cliente"
_POR_ID = f"{_SELECT} WHERE poliza_id = %s"
_POR_CLIENTE = f"{_SELECT} WHERE cliente_id = %s ORDER BY vigencia_hasta DESC, numero"


def poliza_de(fila: tuple[Any, ...]) -> PolizaProyectada:
    datos = dict(zip(COLUMNAS, fila, strict=True))
    return PolizaProyectada(
        poliza_id=datos["poliza_id"],
        cliente_id=datos["cliente_id"],
        numero=datos["numero"],
        estado=datos["estado"],
        producto_id=datos["producto_id"],
        producto_version=datos["producto_version"],
        producto_nombre=datos["producto_nombre"],
        ramo=datos["ramo"],
        vigencia=Vigencia(datos["vigencia_desde"], datos["vigencia_hasta"]),
        prima=datos["prima"],
        suma_asegurada=datos["suma_asegurada"],
        moneda=datos["moneda"],
        periodicidad=datos["periodicidad"],
        coberturas=tuple(
            CoberturaProyectada(c["codigo"], c["nombre"], c["obligatoria"])
            for c in datos["coberturas"]
        ),
        documento_huella=datos["documento_huella"],
        firma_id=datos["firma_id"],
        emitida_en=datos["emitida_en"],
        proyectada_en=datos["proyectada_en"],
    )


class LecturaPolizasPg:
    """Implementa LecturaPolizas sobre el pool."""

    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    async def por_id(self, poliza_id: UUID) -> PolizaProyectada | None:
        async with self._pool.connection() as conexion:
            cursor = await conexion.cursor(row_factory=tuple_row).execute(_POR_ID, (poliza_id,))
            fila = await cursor.fetchone()
        return poliza_de(fila) if fila else None

    async def por_cliente(self, cliente_id: UUID) -> list[PolizaProyectada]:
        async with self._pool.connection() as conexion:
            cursor = await conexion.cursor(row_factory=tuple_row).execute(
                _POR_CLIENTE, (cliente_id,)
            )
            return [poliza_de(f) for f in await cursor.fetchall()]
