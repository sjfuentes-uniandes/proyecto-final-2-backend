"""Modelo canónico del envío de un aviso push al proveedor (aliado `push`, HU-M11). Puro: sin E/S.

El mensaje es solo de datos (`tokenDispositivo`, `tipo`, `polizaId`, `eventId`); `eventId` es la
clave de colapso del lado de la app (decisiones HU-M11). Sin reintentos aquí: `avisos` los gestiona
con su barrido, así que una sola llamada basta.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class ResultadoPush(StrEnum):
    """La única negativa definitiva del proveedor: un token de dispositivo no reconocido."""

    OK = "OK"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class SolicitudPush:
    """`token_dispositivo` es secreto: nunca va en logs, métricas, etiquetas, URLs ni errores."""

    token_dispositivo: str = field(repr=False)
    tipo: str
    poliza_id: UUID
    event_id: UUID


@dataclass(frozen=True, slots=True)
class Push:
    resultado: ResultadoPush
    envio_id: str | None = None


class RespuestaInvalida(Exception):
    """La respuesta del proveedor no cumple el formato esperado."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
