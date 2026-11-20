"""Registro de eventos recibidos y Outbox en PostgreSQL (HU-W21): tablas de
`migraciones/001_telemetria.sql`.

`registrar` hace en **una transacción** lo que el issue pide («valida y escribe el evento en su
Outbox en la misma transacción»): inserta el evento en `eventos_recibidos` y, solo si es nuevo,
`EventoParametricoObservado` en el `outbox`. Si el evento ya estaba (`ON CONFLICT`), no escribe otra
fila de Outbox y devuelve el `recibido_en` del primero. El relay de `solventa_common.outbox`
publica después del commit. Una falla de la base se traduce a `DependenciaNoDisponible`, sin su
mensaje (puede traer datos).
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from adaptador_telemetria.adapters.outbound.eventos import sobre_de
from adaptador_telemetria.application.telemetria.puertos import (
    DependenciaNoDisponible,
    Registrado,
)
from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("adaptador_telemetria.registro")

DEPENDENCIA = "base"
MENSAJE_FALLO = "base: no se pudo leer ni registrar el evento"

_INSERTAR = (
    "INSERT INTO eventos_recibidos (evento_id, fuente, id_externo, evento_origen, archivo_ref, "
    "correlation_id, recibido_en) VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT DO NOTHING RETURNING recibido_en"
)
_RECIBIDO = "SELECT recibido_en FROM eventos_recibidos WHERE evento_id = %s"
_OUTBOX = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


class RegistroPg:
    """Implementa `Registro`. Toma una conexión del pool solo durante cada consulta."""

    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    async def recibido(self, evento_id: UUID) -> datetime | None:
        try:
            async with self._pool.connection() as conexion:
                cursor = await conexion.execute(_RECIBIDO, (evento_id,))
                fila = await cursor.fetchone()
        except (psycopg.Error, OSError) as error:
            self._fallo(error)
        return fila[0] if fila else None

    async def registrar(
        self, evento: EventoCanonico, recibido_en: datetime, archivo_ref: str, correlation_id: str
    ) -> Registrado:
        evento_id = evento.evento_id
        try:
            async with self._pool.connection() as conexion, conexion.transaction():
                cursor = await conexion.execute(
                    _INSERTAR,
                    (
                        evento_id,
                        evento.fuente,
                        evento.id_externo,
                        evento.evento_origen,
                        archivo_ref,
                        correlation_id,
                        recibido_en,
                    ),
                )
                nuevo = await cursor.fetchone()
                if nuevo is None:
                    # Ya estaba (un reenvío, o dos envíos a la vez): sin otra fila de Outbox.
                    cursor = await conexion.execute(_RECIBIDO, (evento_id,))
                    existente = await cursor.fetchone()
                    if existente is None:  # pragma: no cover - el conflicto implica la fila
                        raise DependenciaNoDisponible(DEPENDENCIA)
                    return Registrado(nuevo=False, recibido_en=existente[0])
                sobre = sobre_de(evento, recibido_en, archivo_ref, correlation_id)
                await conexion.execute(
                    _OUTBOX,
                    (
                        sobre.event_id,
                        sobre.aggregate_id,
                        sobre.type,
                        sobre.version,
                        sobre.occurred_at,
                        sobre.correlation_id,
                        Jsonb(sobre.payload.model_dump(mode="json", by_alias=True)),
                    ),
                )
                return Registrado(nuevo=True, recibido_en=nuevo[0])
        except (psycopg.Error, OSError) as error:
            self._fallo(error)

    @staticmethod
    def _fallo(error: BaseException) -> NoReturn:
        # Solo el tipo: el mensaje de la base puede traer datos de la fila.
        logger.error(MENSAJE_FALLO, extra={"error_type": type(error).__name__})
        raise DependenciaNoDisponible(DEPENDENCIA) from None


class RegistroSinConfigurar:
    """Sin base configurada (local sin PostgreSQL): un evento sin registro no se acepta."""

    async def recibido(self, evento_id: UUID) -> datetime | None:
        raise DependenciaNoDisponible(DEPENDENCIA)

    async def registrar(
        self, evento: EventoCanonico, recibido_en: datetime, archivo_ref: str, correlation_id: str
    ) -> Registrado:
        raise DependenciaNoDisponible(DEPENDENCIA)
