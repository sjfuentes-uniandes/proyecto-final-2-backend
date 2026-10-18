"""Decisión de la verificación del consentimiento (HU-W31; ADR 0010 y 0011). Pura: sin E/S.

La base devuelve en una sola consulta la fila y su instante (`ahora`); aquí se decide con
`esta_vigente`, de modo que los límites exactos se prueban sin base de datos.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from clientes.domain.consentimientos.modelos import (
    Consentimiento,
    EstadoConsentimiento,
    esta_vigente,
)


class Decision(StrEnum):
    PERMITIDO = "PERMITIDO"
    PERMITIDO_PARCIAL = "PERMITIDO_PARCIAL"
    RECHAZADO = "RECHAZADO"


class CausaRechazo(StrEnum):
    """Causa funcional estable de un `RECHAZADO` (W31 AC2)."""

    SIN_CONSENTIMIENTO = "SIN_CONSENTIMIENTO"
    REVOCADO = "REVOCADO"
    VENCIDO = "VENCIDO"
    PROPOSITO_NO_AUTORIZADO = "PROPOSITO_NO_AUTORIZADO"


class CausaAlcance(StrEnum):
    """Causa de cada alcance rechazado (W31 AC3)."""

    ALCANCE_NO_AUTORIZADO = "ALCANCE_NO_AUTORIZADO"


@dataclass(frozen=True, slots=True)
class SolicitudVerificacion:
    proposito: str
    alcances: tuple[str, ...]

    @classmethod
    def crear(cls, proposito: str, alcances: Sequence[str]) -> "SolicitudVerificacion":
        """Los alcances sin repetir y en el orden en que se pidieron."""
        return cls(proposito, tuple(dict.fromkeys(alcances)))


@dataclass(frozen=True, slots=True)
class LecturaVerificacion:
    """Lo que devuelve la consulta de la verificación, con una sola captura del reloj.

    `consentimiento` es el más reciente del propósito pedido (None si nunca hubo uno);
    `otros_activos_hasta`, el `vigente_hasta` de cada `ACTIVO` almacenado de otro propósito.
    """

    cliente_id: UUID
    ahora: datetime
    consentimiento: Consentimiento | None
    otros_activos_hasta: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class Verificacion:
    """La decisión que se responde y se registra.

    `consentimiento_id` y `vigente_hasta` son los del consentimiento que autoriza la consulta:
    nulos en un rechazo. `consentimiento_evaluado` es el que se examinó, también si estaba revocado
    o vencido; solo va al registro de auditoría, nunca a la respuesta.
    """

    cliente_id: UUID
    proposito: str
    alcances_solicitados: tuple[str, ...]
    decision: Decision
    causa: CausaRechazo | None
    alcances_permitidos: tuple[str, ...]
    alcances_rechazados: tuple[str, ...]
    consentimiento_id: UUID | None
    vigente_hasta: datetime | None
    consentimiento_evaluado: UUID | None
    verificado_en: datetime


def decidir(solicitud: SolicitudVerificacion, lectura: LecturaVerificacion) -> Verificacion:
    """Vigente (`esta_vigente` con el `ahora` de la base): cada alcance pedido se permite si el
    consentimiento lo incluye y se rechaza si no (AC1, AC3). No vigente: `RECHAZADO` con todos los
    alcances rechazados y una causa estable (AC2, AC4)."""
    consentimiento = lectura.consentimiento
    if consentimiento is not None and esta_vigente(
        consentimiento.estado, consentimiento.vigente_hasta, lectura.ahora
    ):
        permitidos = tuple(a for a in solicitud.alcances if a in consentimiento.alcances)
        rechazados = tuple(a for a in solicitud.alcances if a not in consentimiento.alcances)
        return Verificacion(
            cliente_id=lectura.cliente_id,
            proposito=solicitud.proposito,
            alcances_solicitados=solicitud.alcances,
            decision=Decision.PERMITIDO_PARCIAL if rechazados else Decision.PERMITIDO,
            causa=None,
            alcances_permitidos=permitidos,
            alcances_rechazados=rechazados,
            consentimiento_id=consentimiento.id,
            vigente_hasta=consentimiento.vigente_hasta,
            consentimiento_evaluado=consentimiento.id,
            verificado_en=lectura.ahora,
        )
    return Verificacion(
        cliente_id=lectura.cliente_id,
        proposito=solicitud.proposito,
        alcances_solicitados=solicitud.alcances,
        decision=Decision.RECHAZADO,
        causa=_causa(lectura),
        alcances_permitidos=(),
        alcances_rechazados=solicitud.alcances,
        consentimiento_id=None,
        vigente_hasta=None,
        consentimiento_evaluado=consentimiento.id if consentimiento is not None else None,
        verificado_en=lectura.ahora,
    )


def _causa(lectura: LecturaVerificacion) -> CausaRechazo:
    """Sin consentimiento del propósito: `PROPOSITO_NO_AUTORIZADO` si hay otro propósito vigente
    y `SIN_CONSENTIMIENTO` si no. Con uno no vigente: manda el estado almacenado; un `ACTIVO` que
    ya pasó su `vigente_hasta` es `VENCIDO`."""
    consentimiento = lectura.consentimiento
    if consentimiento is None:
        otro_vigente = any(
            esta_vigente(EstadoConsentimiento.ACTIVO, hasta, lectura.ahora)
            for hasta in lectura.otros_activos_hasta
        )
        return (
            CausaRechazo.PROPOSITO_NO_AUTORIZADO
            if otro_vigente
            else CausaRechazo.SIN_CONSENTIMIENTO
        )
    if consentimiento.estado is EstadoConsentimiento.REVOCADO:
        return CausaRechazo.REVOCADO
    return CausaRechazo.VENCIDO
