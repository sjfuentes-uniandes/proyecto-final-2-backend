"""Modelo canónico de la firma de un documento con el proveedor de firma. Puro: sin E/S.

Se firma la huella sha256 del documento, no el documento (ADR 0014, decisión 5). El firmante es
una referencia sintética (el `clienteId`): ningún dato personal.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class EstadoFirma(StrEnum):
    """La única respuesta definitiva del proveedor: no hay rechazo (plan W17, §b, decisión 9)."""

    FIRMADO = "FIRMADO"


@dataclass(frozen=True, slots=True)
class SolicitudFirma:
    documento_id: UUID
    huella: str
    firmante_ref: UUID


@dataclass(frozen=True, slots=True)
class Firma:
    documento_id: UUID
    estado: EstadoFirma
    firma_id: str
    sello_tiempo: datetime
    huella: str


class RespuestaInvalida(Exception):
    """La respuesta del proveedor no cumple el formato esperado o no es la de lo enviado."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
