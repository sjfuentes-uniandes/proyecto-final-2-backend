"""Modelo canónico de la verificación de identidad. Puro: sin E/S."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class ResultadoVerificacion(StrEnum):
    APROBADO = "APROBADO"
    CAPTURA_INVALIDA = "CAPTURA_INVALIDA"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class Verificacion:
    resultado: ResultadoVerificacion
    motivo: str | None


class ResultadoDocumento(StrEnum):
    """Veredicto de la verificación documental: sin captura, solo aprueba o rechaza."""

    APROBADO = "APROBADO"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class DatosDocumento:
    tipo_documento: str
    numero_documento: str
    nombre_completo: str
    fecha_nacimiento: date


@dataclass(frozen=True, slots=True)
class VerificacionDocumento:
    resultado: ResultadoDocumento
    motivo: str | None
    referencia: str
    proveedor: str


class RespuestaInvalida(Exception):
    """La respuesta del proveedor no cumple el formato esperado."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
