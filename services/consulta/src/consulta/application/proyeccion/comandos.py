"""El hecho que proyecta la HU-M10: una póliza emitida, ya validada por el adaptador de entrada."""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PolizaEmitida:
    poliza_id: UUID
    cliente_id: UUID
    numero: str
    producto_id: str
    producto_version: str
    vigencia_desde: date
    vigencia_hasta: date
    prima: str
    suma_asegurada: str
    moneda: str
    periodicidad: str
    documento_huella: str
    firma_id: str
    emitida_en: datetime
