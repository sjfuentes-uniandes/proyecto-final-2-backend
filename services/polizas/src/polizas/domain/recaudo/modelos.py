"""Estado del recaudo de la prima, visto desde polizas (HU-W16, 16.13; ADR 0013, decisión 9).

`dinero` es el dueño del recaudo; polizas solo proyecta el primer estado final que publica
(`RecaudoConfirmado` o `RecaudoRechazado`) por `claveRecaudo`, que es el id de la aceptación.
Mientras no llega ninguno, el recaudo de una aceptación está PENDIENTE con la prima de la
instantánea de la decisión (DS2-10). Puros: sin Pydantic ni E/S.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from polizas.domain.aceptacion.modelos import Aceptacion
from polizas.domain.suscripcion.modelos import Decision


class SituacionRecaudo(StrEnum):
    PENDIENTE = "PENDIENTE"
    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class ResultadoRecaudo:
    """Un estado final publicado por dinero: CONFIRMADO (con `transaccion_id`, monto y moneda) o
    RECHAZADO (con `motivo`; `RecaudoRechazado` v1 no trae el monto)."""

    clave_recaudo: UUID
    recaudo_id: UUID
    estado: SituacionRecaudo
    monto: Decimal | None
    moneda: str | None
    transaccion_id: str | None
    motivo: str | None
    ocurrido_en: datetime


@dataclass(frozen=True, slots=True)
class EstadoRecaudo:
    """Lo que se lee del recaudo de una aceptación."""

    clave_recaudo: UUID
    estado: SituacionRecaudo
    monto: Decimal
    moneda: str
    transaccion_id: str | None
    motivo: str | None
    actualizado_en: datetime


def estado_recaudo(
    aceptacion: Aceptacion, decision: Decision, resultado: ResultadoRecaudo | None
) -> EstadoRecaudo:
    """El estado del recaudo de la aceptación: el resultado proyectado o, sin él, PENDIENTE. El
    monto y la moneda que no trae el resultado salen de la instantánea de la decisión."""
    insumos = decision.insumos
    if resultado is None:
        return EstadoRecaudo(
            clave_recaudo=aceptacion.id,
            estado=SituacionRecaudo.PENDIENTE,
            monto=insumos.prima_total,
            moneda=insumos.moneda,
            transaccion_id=None,
            motivo=None,
            actualizado_en=aceptacion.aceptada_en,
        )
    if resultado.monto is None or resultado.moneda is None:
        monto, moneda = insumos.prima_total, insumos.moneda
    else:
        monto, moneda = resultado.monto, resultado.moneda
    return EstadoRecaudo(
        clave_recaudo=aceptacion.id,
        estado=resultado.estado,
        monto=monto,
        moneda=moneda,
        transaccion_id=resultado.transaccion_id,
        motivo=resultado.motivo,
        actualizado_en=resultado.ocurrido_en,
    )
