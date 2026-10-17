"""Texto legal versionado del consentimiento (ADR 0005). Puro: sin E/S."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextoLegal:
    """Texto ilustrativo, rotulado en `aviso` como "sin validez legal".

    Cambiar el contenido exige una `version` nueva; no cambia el contrato ni la app.
    """

    proposito: str
    idioma: str
    version: str
    titulo: str
    cuerpo: str
    aviso: str
    vigencias_meses: tuple[int, ...]
    vigencia_defecto_meses: int
