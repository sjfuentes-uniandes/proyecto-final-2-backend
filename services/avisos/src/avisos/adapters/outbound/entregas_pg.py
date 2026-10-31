"""RepositorioEntregas en PostgreSQL (HU-M11): tablas `dispositivo` y `entrega` de 001.

`crear_pendientes` escribe en la transacción del inbox. El resto abre su propia conexión del pool.
`reclamar` es una sola sentencia: el `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)`
toma la entrega y suma el intento de forma atómica, así que dos tareas nunca reclaman la misma. Las
marcas posteriores exigen `estado = 'EN_ENVIO'`: si el lease venció y otro la retomó, la marca tardía
de un proceso lento no pisa el resultado. Las migraciones las aplica `migrar` (todos los .sql en orden).
"""

from datetime import datetime
from importlib import resources
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from solventa_common.inbox import TransaccionInboxPg

from avisos.domain.avisos.modelos import (
    FALLO_LEASE_VENCIDO,
    Aviso,
    Entrega,
    es_codigo,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_PAQUETE = resources.files("avisos.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('avisos.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('avisos.migraciones'))"

_CREAR = (
    "INSERT INTO entrega (event_id, dispositivo_id, tipo, poliza_id, estado, proximo_intento,"
    " creada_en, actualizada_en) "
    "SELECT %(event_id)s, d.id, %(tipo)s, %(poliza_id)s, 'PENDIENTE', %(ahora)s, %(ahora)s, %(ahora)s "
    "FROM dispositivo d WHERE d.cliente_id = %(cliente_id)s AND d.estado = 'ACTIVO' "
    "ON CONFLICT (event_id, dispositivo_id) DO NOTHING RETURNING id"
)
_RECLAMAR = (
    "WITH tomada AS ("
    " UPDATE entrega SET estado = 'EN_ENVIO', reclamada_hasta = %(hasta)s,"
    " intentos = intentos + 1, actualizada_en = %(ahora)s"
    " WHERE id = (SELECT id FROM entrega"
    "  WHERE intentos < %(max_intentos)s"
    "   AND (%(event_id)s::uuid IS NULL OR event_id = %(event_id)s::uuid)"
    "   AND ((estado = 'PENDIENTE' AND proximo_intento <= %(ahora)s)"
    "     OR (estado = 'EN_ENVIO' AND reclamada_hasta < %(ahora)s))"
    "  ORDER BY proximo_intento LIMIT 1 FOR UPDATE SKIP LOCKED)"
    " RETURNING id, dispositivo_id, event_id, tipo, poliza_id, intentos) "
    "SELECT t.id, t.dispositivo_id, t.event_id, t.tipo, t.poliza_id, t.intentos,"
    " d.token, (d.estado = 'ACTIVO') AS activo "
    "FROM tomada t JOIN dispositivo d ON d.id = t.dispositivo_id"
)
_ENVIADA = (
    "UPDATE entrega SET estado = 'ENVIADO', reclamada_hasta = NULL, ultimo_fallo = NULL,"
    " actualizada_en = %s WHERE id = %s AND estado = 'EN_ENVIO'"
)
_ENTREGA_TOKEN_INVALIDO = (
    "UPDATE entrega SET estado = 'TOKEN_INVALIDO', reclamada_hasta = NULL, ultimo_fallo = %s,"
    " actualizada_en = %s WHERE id = %s AND estado = 'EN_ENVIO'"
)
_DISPOSITIVO_INVALIDO = (
    "UPDATE dispositivo SET estado = 'INVALIDO', invalidado_en = %s, motivo_invalidez = %s,"
    " actualizado_en = %s WHERE id = %s AND estado = 'ACTIVO'"
)
_REPROGRAMAR = (
    "UPDATE entrega SET estado = 'PENDIENTE', reclamada_hasta = NULL, proximo_intento = %s,"
    " ultimo_fallo = %s, actualizada_en = %s WHERE id = %s AND estado = 'EN_ENVIO'"
)
_FALLIDA = (
    "UPDATE entrega SET estado = 'FALLIDO', reclamada_hasta = NULL, ultimo_fallo = %s,"
    " actualizada_en = %s WHERE id = %s AND estado = 'EN_ENVIO'"
)
_AGOTAR = (
    "UPDATE entrega SET estado = 'FALLIDO', reclamada_hasta = NULL, ultimo_fallo = %s,"
    " actualizada_en = %s WHERE estado = 'EN_ENVIO' AND reclamada_hasta < %s AND intentos >= %s"
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


def _codigo(valor: str) -> str:
    if not es_codigo(valor):
        raise ValueError("el código debe ser una constante")
    return valor


class EntregasPg:
    """Implementa RepositorioEntregas[TransaccionInboxPg]."""

    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    async def crear_pendientes(
        self, aviso: Aviso, cliente_id: UUID, ahora: datetime, tx: TransaccionInboxPg
    ) -> int:
        cursor = await tx.conexion.execute(
            _CREAR,
            {
                "event_id": aviso.event_id,
                "tipo": aviso.tipo,
                "poliza_id": aviso.poliza_id,
                "cliente_id": cliente_id,
                "ahora": ahora,
            },
        )
        return len(await cursor.fetchall())

    async def reclamar(
        self,
        ahora: datetime,
        hasta: datetime,
        max_intentos: int,
        event_id: UUID | None = None,
    ) -> Entrega | None:
        async with self._pool.connection() as conexion:
            cursor = await conexion.cursor(row_factory=dict_row).execute(
                _RECLAMAR,
                {
                    "ahora": ahora,
                    "hasta": hasta,
                    "max_intentos": max_intentos,
                    "event_id": event_id,
                },
            )
            fila = await cursor.fetchone()
        if fila is None:
            return None
        return Entrega(
            id=fila["id"],
            dispositivo_id=fila["dispositivo_id"],
            aviso=Aviso(tipo=fila["tipo"], poliza_id=fila["poliza_id"], event_id=fila["event_id"]),
            intentos=fila["intentos"],
            dispositivo_activo=fila["activo"],
            token=fila["token"],
        )

    async def marcar_enviada(self, entrega_id: UUID, ahora: datetime) -> None:
        async with self._pool.connection() as conexion:
            await conexion.execute(_ENVIADA, (ahora, entrega_id))

    async def marcar_token_invalido(self, entrega: Entrega, ahora: datetime, motivo: str) -> None:
        async with self._pool.connection() as conexion, conexion.transaction():
            await conexion.execute(_ENTREGA_TOKEN_INVALIDO, (_codigo(motivo), ahora, entrega.id))
            await conexion.execute(
                _DISPOSITIVO_INVALIDO, (ahora, _codigo(motivo), ahora, entrega.dispositivo_id)
            )

    async def reprogramar(
        self, entrega_id: UUID, proximo_intento: datetime, codigo: str, ahora: datetime
    ) -> None:
        async with self._pool.connection() as conexion:
            await conexion.execute(
                _REPROGRAMAR, (proximo_intento, _codigo(codigo), ahora, entrega_id)
            )

    async def marcar_fallida(self, entrega_id: UUID, codigo: str, ahora: datetime) -> None:
        async with self._pool.connection() as conexion:
            await conexion.execute(_FALLIDA, (_codigo(codigo), ahora, entrega_id))

    async def agotar_vencidas(self, ahora: datetime, max_intentos: int) -> int:
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(
                _AGOTAR, (FALLO_LEASE_VENCIDO, ahora, ahora, max_intentos)
            )
            return cursor.rowcount
