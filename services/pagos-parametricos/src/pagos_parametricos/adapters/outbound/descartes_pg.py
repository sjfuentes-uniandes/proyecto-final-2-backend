"""Causas, resoluciones y Outbox de la cola de descarte en PostgreSQL (HU-W24): tablas de
`migraciones/001_descartes.sql`.

- `RegistroFallosPg.al_fallar` es el gancho `al_fallar` de `solventa_common.inbox.ConsumidorInbox`
  para el consumidor de `parametricos-eventos` (HU-W21 lo conecta): guarda la causa por `MessageId`
  en su propia conexión, fuera de la transacción del mensaje (que ya hizo rollback).
- `ResolucionesPg.registrar` escribe la resolución y su `DescarteParametricoResuelto` en el Outbox
  en una transacción. Si el mensaje ya tenía resolución, la devuelve sin escribir.
"""

from collections.abc import Sequence
from importlib import resources
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from solventa_common.inbox import Desenlace, EventoRecibido, MensajeSqs

from pagos_parametricos.adapters.outbound.eventos import (
    DESCARTE_PARAMETRICO_RESUELTO,
    DescarteParametricoResueltoV1,
)
from pagos_parametricos.application.descartes import Reloj, Resolucion
from pagos_parametricos.domain.descarte import Accion, Causa, FalloAnotado

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_PAQUETE = resources.files("pagos_parametricos.adapters.outbound")
_LOCK = "SELECT pg_advisory_lock(hashtext('pagos_parametricos.migraciones'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('pagos_parametricos.migraciones'))"

_ANOTAR = (
    "INSERT INTO fallos_consumo (message_id, event_id, event_type, outcome, detalle, recepciones, "
    "ultimo_en) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (message_id) DO UPDATE SET "
    "event_id = EXCLUDED.event_id, event_type = EXCLUDED.event_type, outcome = EXCLUDED.outcome, "
    "detalle = EXCLUDED.detalle, recepciones = EXCLUDED.recepciones, ultimo_en = EXCLUDED.ultimo_en"
)
_ANOTADOS = (
    "SELECT message_id, outcome, detalle, recepciones FROM fallos_consumo "
    "WHERE message_id = ANY(%s)"
)
_COLUMNAS = (
    "message_id, evento_id, accion, causa, actor_id, motivo, original, corregido, resuelto_en"
)
_RESOLVER = (
    f"INSERT INTO resoluciones_descarte ({_COLUMNAS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (message_id) DO NOTHING RETURNING message_id"
)
_RESOLUCION = f"SELECT {_COLUMNAS} FROM resoluciones_descarte WHERE message_id = %s"
_OUTBOX = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
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


class RegistroFallosPg:
    """Implementa `Fallos` y el gancho `al_fallar` del consumidor."""

    def __init__(self, pool: "AsyncConnectionPool[Any]", reloj: Reloj) -> None:
        self._pool = pool
        self._reloj = reloj

    async def al_fallar(
        self, mensaje: MensajeSqs, evento: EventoRecibido | None, desenlace: Desenlace
    ) -> None:
        async with self._pool.connection() as conexion:
            await conexion.execute(
                _ANOTAR,
                (
                    mensaje.message_id,
                    evento.event_id if evento else None,
                    evento.event_type if evento else None,
                    desenlace.outcome,
                    desenlace.error_type or desenlace.reason,
                    mensaje.recepciones,
                    self._reloj.ahora(),
                ),
            )

    async def anotados(self, message_ids: Sequence[str]) -> dict[str, FalloAnotado]:
        if not message_ids:
            return {}
        async with self._pool.connection() as conexion:
            cursor = await conexion.execute(_ANOTADOS, (list(message_ids),))
            filas = await cursor.fetchall()
        return {fila[0]: FalloAnotado(fila[1], fila[2], fila[3]) for fila in filas}


def _resolucion(fila: Sequence[Any]) -> Resolucion:
    return Resolucion(
        message_id=fila[0],
        evento_id=fila[1],
        accion=Accion(fila[2]),
        causa=Causa(fila[3]),
        actor_id=fila[4],
        motivo=fila[5],
        original=fila[6],
        corregido=fila[7],
        resuelto_en=fila[8],
    )


def agregado_de(resolucion: Resolucion) -> UUID:
    """El `eventId` resuelto; si el original no traía uno válido, el `MessageId` (un uuid)."""
    for candidato in (resolucion.evento_id, resolucion.message_id):
        try:
            return UUID(str(candidato))
        except ValueError:
            continue
    raise ValueError("ni el eventId ni el MessageId son uuid")


def payload_de(resolucion: Resolucion) -> dict[str, Any]:
    return DescarteParametricoResueltoV1(
        message_id=resolucion.message_id,
        evento_id=resolucion.evento_id,
        accion=resolucion.accion,
        causa=resolucion.causa,
        actor_id=resolucion.actor_id,
        motivo=resolucion.motivo,
        original=resolucion.original,
        corregido=resolucion.corregido,
        resuelto_en=resolucion.resuelto_en,
    ).model_dump(mode="json", by_alias=True)


class ResolucionesPg:
    def __init__(self, pool: "AsyncConnectionPool[Any]") -> None:
        self._pool = pool

    async def registrar(self, resolucion: Resolucion, correlation_id: str) -> Resolucion:
        async with self._pool.connection() as conexion, conexion.transaction():
            cursor = await conexion.execute(
                _RESOLVER,
                (
                    resolucion.message_id,
                    resolucion.evento_id,
                    resolucion.accion.value,
                    resolucion.causa.value,
                    resolucion.actor_id,
                    resolucion.motivo,
                    resolucion.original,
                    resolucion.corregido,
                    resolucion.resuelto_en,
                ),
            )
            if await cursor.fetchone() is None:
                previa = await (
                    await conexion.execute(_RESOLUCION, (resolucion.message_id,))
                ).fetchone()
                assert previa is not None
                return _resolucion(previa)
            await conexion.execute(
                _OUTBOX,
                (
                    uuid4(),
                    agregado_de(resolucion),
                    DESCARTE_PARAMETRICO_RESUELTO,
                    1,
                    resolucion.resuelto_en,
                    correlation_id,
                    Jsonb(payload_de(resolucion)),
                ),
            )
        return resolucion

    async def de(self, message_id: str) -> Resolucion | None:
        async with self._pool.connection() as conexion:
            fila = await (await conexion.execute(_RESOLUCION, (message_id,))).fetchone()
        return _resolucion(fila) if fila else None
