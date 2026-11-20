"""Modelo canónico del desembolso con el proveedor de pagos (HU-W23). Puro: sin E/S.

Es la orden de pago de una indemnización (ADR 0015). Sin datos de cuenta: el beneficiario es una
referencia opaca (`beneficiarioRef`) y el proveedor emula una cuenta destino ya registrada.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class EstadoDesembolso(StrEnum):
    """Respuesta definitiva del proveedor: solo estas dos cambian la indemnización en `dinero`."""

    CONFIRMADO = "CONFIRMADO"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class SolicitudDesembolso:
    referencia: str
    monto: Decimal
    moneda: str
    beneficiario_ref: str


@dataclass(frozen=True, slots=True)
class Desembolso:
    referencia: str
    estado: EstadoDesembolso
    transaccion_id: str | None
    motivo: str | None


class DesembolsoNoEncontrado(Exception):
    """El proveedor no tiene una orden con esa referencia y ese beneficiario
    (`desembolso_no_encontrado`)."""
