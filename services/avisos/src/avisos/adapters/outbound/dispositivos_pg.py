"""RepositorioDispositivos en PostgreSQL (HU-M11): registro, baja y reasignación de dispositivos.

Cada método abre su propia conexión y transacción; ambos (`registrar` y `dar_de_baja`) bloquean la
fila del dispositivo (`FOR UPDATE`) antes de decidir, así que dos solicitudes concurrentes del mismo
token o del mismo dispositivo nunca se pisan. La baja y la reasignación fallan, en la misma
transacción que cambia el dueño, las entregas PENDIENTE o EN_ENVIO de ese dispositivo: así nunca
queda un aviso pendiente para el cliente equivocado.

El token nunca se registra ni se expone en logs, métricas, URLs o errores; su identidad es el hash
(sha256), que garantiza un solo dispositivo por token.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from avisos.application.dispositivos.puertos import DispositivoNoAutorizado, DispositivoNoEncontrado
from avisos.domain.avisos.modelos import (
    FALLO_DISPOSITIVO_DADO_DE_BAJA,
    FALLO_DISPOSITIVO_REASIGNADO,
    MOTIVO_BAJA_CLIENTE,
    hash_token,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_BUSCAR_POR_HASH = "SELECT id, cliente_id FROM dispositivo WHERE token_hash = %s FOR UPDATE"
_INSERTAR = (
    "INSERT INTO dispositivo (cliente_id, token_hash, token, estado, registrado_en, actualizado_en)"
    " VALUES (%s, %s, %s, 'ACTIVO', %s, %s) RETURNING id"
)
# Siempre deja el dispositivo ACTIVO y sin invalidez: cubre el no-op (ya activo, mismo cliente), la
# reactivación (estaba INVALIDO) y la reasignación (cliente_id cambia) con la misma sentencia.
_REGISTRAR_EXISTENTE = (
    "UPDATE dispositivo SET cliente_id = %s, token = %s, estado = 'ACTIVO', invalidado_en = NULL,"
    " motivo_invalidez = NULL, actualizado_en = %s WHERE id = %s"
)
_BUSCAR_POR_ID = "SELECT id, cliente_id FROM dispositivo WHERE id = %s FOR UPDATE"
_INVALIDAR = (
    "UPDATE dispositivo SET estado = 'INVALIDO', invalidado_en = %s, motivo_invalidez = %s,"
    " actualizado_en = %s WHERE id = %s"
)
_FALLAR_ENTREGAS_DEL_DISPOSITIVO = (
    "UPDATE entrega SET estado = 'FALLIDO', reclamada_hasta = NULL, ultimo_fallo = %s,"
    " actualizada_en = %s WHERE dispositivo_id = %s AND estado IN ('PENDIENTE', 'EN_ENVIO')"
)


class DispositivosPg:
    """Implementa RepositorioDispositivos."""

    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    async def registrar(self, cliente_id: UUID, token: str, ahora: datetime) -> UUID:
        token_hash = hash_token(token)
        async with self._pool.connection() as conexion, conexion.transaction():
            cursor = await conexion.execute(_BUSCAR_POR_HASH, (token_hash,))
            fila = await cursor.fetchone()
            if fila is None:
                cursor = await conexion.execute(
                    _INSERTAR, (cliente_id, token_hash, token, ahora, ahora)
                )
                creado = await cursor.fetchone()
                if creado is None:
                    raise RuntimeError("INSERT ... RETURNING no devolvió fila")
                return UUID(str(creado[0]))
            dispositivo_id, cliente_anterior = fila
            await conexion.execute(_REGISTRAR_EXISTENTE, (cliente_id, token, ahora, dispositivo_id))
            if cliente_anterior != cliente_id:
                await conexion.execute(
                    _FALLAR_ENTREGAS_DEL_DISPOSITIVO,
                    (FALLO_DISPOSITIVO_REASIGNADO, ahora, dispositivo_id),
                )
            return UUID(str(dispositivo_id))

    async def dar_de_baja(self, dispositivo_id: UUID, cliente_id: UUID, ahora: datetime) -> None:
        async with self._pool.connection() as conexion, conexion.transaction():
            cursor = await conexion.execute(_BUSCAR_POR_ID, (dispositivo_id,))
            fila = await cursor.fetchone()
            if fila is None:
                raise DispositivoNoEncontrado
            _id, cliente_actual = fila
            if cliente_actual != cliente_id:
                raise DispositivoNoAutorizado
            await conexion.execute(_INVALIDAR, (ahora, MOTIVO_BAJA_CLIENTE, ahora, dispositivo_id))
            await conexion.execute(
                _FALLAR_ENTREGAS_DEL_DISPOSITIVO,
                (FALLO_DISPOSITIVO_DADO_DE_BAJA, ahora, dispositivo_id),
            )
