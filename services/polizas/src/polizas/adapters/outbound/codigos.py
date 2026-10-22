"""Código legible de la decisión, DEC-AAAA-NNNNNN, desde una secuencia de la base (HU-W13).

La secuencia es global: el número no se repite entre años ni entre réplicas. Un número que no
llega a usarse (otra solicitud ganó la carrera) deja un hueco, que no importa.
"""

from psycopg_pool import AsyncConnectionPool

_SIGUIENTE = "SELECT nextval('decisiones_codigo')"


def codigo(anio: int, numero: int) -> str:
    return f"DEC-{anio:04d}-{numero:06d}"


class CodigosPg:
    """Implementa GeneradorCodigos."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def siguiente(self, anio: int) -> str:
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(_SIGUIENTE)
            fila = await cursor.fetchone()
        assert fila is not None
        numero: int = fila[0]
        return codigo(anio, numero)
