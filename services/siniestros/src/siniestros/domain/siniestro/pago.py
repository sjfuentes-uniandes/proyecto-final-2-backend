"""Pago único de la indemnización de un siniestro aprobado (HU-W23; ADR 0015; DS3-25). Puro.

`dinero` paga una sola vez y publica `IndemnizacionConfirmada` o `IndemnizacionRechazada`;
`siniestros` solo **refleja** el resultado en las columnas `pago_*` (que existen desde HU-W19) y en
el historial. No reemite eventos.

- El **primer resultado gana**: de `CONFIRMADO` y `RECHAZADO` no se sale. Un resultado repetido (un
  `Confirmada` después de otro `Confirmada`) o fuera de orden (un `Rechazada` después de un
  `Confirmada`) no cambia nada.
- Un caso `APROBADO` sin resultado de `dinero` todavía tiene `PENDIENTE` (la orden de pago está en
  camino o la pasarela no ha respondido): se **deriva al leer**, con la referencia prevista y los
  contadores en cero, y no se escribe (R-105). Un caso que no está aprobado no tiene pago (`None`).
- `RECHAZADO` se presenta como «Pago por gestionar» (AC4): el siniestro sigue `APROBADO`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from siniestros.domain.siniestro.modelos import EstadoSiniestro
from siniestros.domain.siniestro.tramite import ACCION_PAGO_CONFIRMADO
from siniestros.domain.siniestro.transiciones import referencia_pago

ACCION_PAGO_RECHAZADO = "PAGO_RECHAZADO"


class EstadoPago(StrEnum):
    # `SIN_PAGO` se conserva por compatibilidad con W19 (`pago_estado` nulo): no se emite.
    SIN_PAGO = "SIN_PAGO"
    PENDIENTE = "PENDIENTE"
    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"


ESTADOS_FINALES_DE_PAGO = frozenset({EstadoPago.CONFIRMADO, EstadoPago.RECHAZADO})


@dataclass(frozen=True)
class ResultadoPago:
    """Lo que `dinero` informó: solo ids, el código de la pasarela y contadores. Sin cuenta."""

    siniestro_id: UUID
    estado: EstadoPago
    referencia: str
    transaccion_id: str | None
    motivo: str | None
    intentos: int
    duplicados_descartados: int
    ocurrido_en: datetime

    def __post_init__(self) -> None:
        if self.estado not in ESTADOS_FINALES_DE_PAGO:
            raise ValueError("estado: un resultado es CONFIRMADO o RECHAZADO")

    @property
    def accion(self) -> str:
        return (
            ACCION_PAGO_CONFIRMADO
            if self.estado is EstadoPago.CONFIRMADO
            else ACCION_PAGO_RECHAZADO
        )


@dataclass(frozen=True)
class PagoSiniestro:
    """El pago tal como lo ve el detalle del caso."""

    estado: EstadoPago
    referencia: str
    transaccion_id: str | None
    motivo: str | None
    intentos: int
    duplicados_descartados: int


def pago_de(
    estado_siniestro: EstadoSiniestro,
    codigo: str,
    guardado: PagoSiniestro | None,
) -> PagoSiniestro | None:
    """`None` si el caso no está aprobado; el pago guardado si `dinero` ya informó; si no,
    `PENDIENTE` derivado (R-105)."""
    if estado_siniestro is not EstadoSiniestro.APROBADO:
        return None
    if guardado is not None:
        return guardado
    return PagoSiniestro(
        estado=EstadoPago.PENDIENTE,
        referencia=referencia_pago(codigo),
        transaccion_id=None,
        motivo=None,
        intentos=0,
        duplicados_descartados=0,
    )


def acepta_resultado(estado_guardado: EstadoPago | None) -> bool:
    """Solo el primer resultado cambia algo: de un estado final no se sale."""
    return estado_guardado is None or estado_guardado not in ESTADOS_FINALES_DE_PAGO
