"""Errores de negocio de la aceptación; la API los traduce a B2 (HU-W15).

`OfertaVencida` es el mismo de la suscripción: una sola noción de oferta vencida (DS2-10).
"""

from uuid import UUID

from polizas.domain.suscripcion.errores import OfertaVencida

__all__ = [
    "AceptacionNoEncontrada",
    "DecisionNoAprobada",
    "DeclaracionesIncompletas",
    "OfertaVencida",
    "OfertaYaAceptada",
    "TextoNoVigente",
]


class TextoNoVigente(Exception):
    """La solicitud trae una versión del texto de las declaraciones que no es la vigente (DS2-24)."""


class DecisionNoAprobada(Exception):
    """La decisión de la cotización es RECHAZADA o REVISION_REQUERIDA."""


class DeclaracionesIncompletas(Exception):
    """Falta alguna declaración obligatoria; `campos` las nombra como en el contrato."""

    def __init__(self, campos: tuple[str, ...]) -> None:
        super().__init__(", ".join(campos))
        self.campos = campos


class OfertaYaAceptada(Exception):
    """La cotización ya tiene una aceptación registrada con otra Idempotency-Key (DS2-11)."""

    def __init__(self, aceptacion_id: UUID) -> None:
        super().__init__(str(aceptacion_id))
        self.aceptacion_id = aceptacion_id


class AceptacionNoEncontrada(Exception):
    """La cotización no tiene aceptación registrada."""
