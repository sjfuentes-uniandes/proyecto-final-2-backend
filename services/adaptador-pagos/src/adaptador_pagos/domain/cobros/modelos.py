"""Modelo canónico del cobro con el proveedor de pagos. Puro: sin E/S.

Sin datos de tarjeta ni de cuenta (ADR 0013, decisión 6): el pagador es una referencia sintética
y el proveedor emula una domiciliación previa.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class EstadoCobro(StrEnum):
    """Respuesta definitiva del proveedor: solo estas dos cambian el movimiento en `dinero`."""

    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class SolicitudCobro:
    clave_recaudo: UUID
    monto: Decimal
    moneda: str
    pagador_ref: UUID


@dataclass(frozen=True, slots=True)
class Cobro:
    clave_recaudo: UUID
    estado: EstadoCobro
    transaccion_id: str | None
    motivo: str | None


class RespuestaInvalida(Exception):
    """La respuesta del proveedor no cumple el formato esperado o tiene un estado no previsto."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class CobroNoEncontrado(Exception):
    """El proveedor no tiene un cobro con esa referencia y ese pagador (`cobro_no_encontrado`)."""
