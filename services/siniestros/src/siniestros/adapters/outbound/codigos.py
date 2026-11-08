"""Consecutivo del código `88-AAAA-NNNN` (DS3-11, R-07). Lo usan los orígenes que crean siniestros
(M01 desde la app, el portal y W22); W19 solo lo deja listo. Arranca en 1000: 0001–0999 queda
reservado a la semilla de QA. Una transacción corta por código."""

from typing import Any

from psycopg_pool import AsyncConnectionPool

from siniestros.domain.siniestro.transiciones import codigo_siniestro

PRIMER_CONSECUTIVO = 1000
_AVANZAR = (
    "INSERT INTO secuencia_codigo (anio, ultimo) VALUES (%s, %s) "
    "ON CONFLICT (anio) DO UPDATE SET ultimo = secuencia_codigo.ultimo + 1 RETURNING ultimo"
)


class CodigosPg:
    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    async def siguiente(self, anio: int) -> str:
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(_AVANZAR, (anio, PRIMER_CONSECUTIVO))
            fila = await cursor.fetchone()
        assert fila is not None
        return codigo_siniestro(anio, int(fila[0]))
