"""Resultado de la verificación biométrica de identidad. Puro: sin E/S."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from bff_movil.domain.sesion.modelos import EstadoIdentidad


class ResultadoKyc(StrEnum):
    APROBADO = "APROBADO"
    CAPTURA_INVALIDA = "CAPTURA_INVALIDA"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class Verificacion:
    cliente_id: UUID
    estado: EstadoIdentidad
    resultado: ResultadoKyc
    motivo: str | None
    verificado_en: datetime | None
