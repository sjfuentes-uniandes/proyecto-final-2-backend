"""Modelo canónico de la verificación de identidad. Puro: sin E/S."""

from dataclasses import dataclass
from enum import StrEnum


class ResultadoVerificacion(StrEnum):
    APROBADO = "APROBADO"
    CAPTURA_INVALIDA = "CAPTURA_INVALIDA"
    RECHAZADO = "RECHAZADO"


@dataclass(frozen=True, slots=True)
class Verificacion:
    resultado: ResultadoVerificacion
    motivo: str | None


class RespuestaInvalida(Exception):
    """La respuesta del proveedor no cumple el formato esperado."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo
