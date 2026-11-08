"""Cadena e inbox de `auditoria` en PostgreSQL (HU-W24): tablas de `migraciones/001_cadena.sql`.

Una transacción por lote: `cabeza` bloquea la única fila de `cadena` (`FOR UPDATE`), `registrar`
inserta los `eventId` en un solo `INSERT … ON CONFLICT DO NOTHING` y `avanzar` mueve la cabeza.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import AsyncConnection

from auditoria.domain.registro import EventoAuditable

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_PAQUETE = resources.files("auditoria.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('auditoria.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('auditoria.migraciones'))"

_CABEZA = "SELECT secuencia, hash FROM cadena WHERE id = 1 FOR UPDATE"
_REGISTRAR = (
    "INSERT INTO inbox (event_id, event_type, recibido_en) "
    "SELECT event_id, event_type, %s FROM unnest(%s::uuid[], %s::text[]) AS e(event_id, event_type) "
    "ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
)
_AVANZAR = "UPDATE cadena SET secuencia = %s, hash = %s, actualizada_en = now() WHERE id = 1"


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


@dataclass(frozen=True)
class TransaccionCadenaPg:
    conexion: AsyncConnection[Any]

    async def cabeza(self) -> tuple[int, str]:
        cursor = await self.conexion.execute(_CABEZA)
        fila = await cursor.fetchone()
        if fila is None:
            raise RuntimeError("la cadena no está inicializada (falta migrar)")
        return int(fila[0]), str(fila[1])

    async def registrar(
        self, eventos: Sequence[EventoAuditable], recibido_en: datetime
    ) -> set[str]:
        cursor = await self.conexion.execute(
            _REGISTRAR,
            (recibido_en, [e.event_id for e in eventos], [e.event_type for e in eventos]),
        )
        return {str(fila[0]) for fila in await cursor.fetchall()}

    async def avanzar(self, secuencia: int, hash_: str) -> None:
        await self.conexion.execute(_AVANZAR, (secuencia, hash_))


class CadenaPg:
    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionCadenaPg]:
        async with self._pool.connection() as conexion, conexion.transaction():
            yield TransaccionCadenaPg(conexion)
