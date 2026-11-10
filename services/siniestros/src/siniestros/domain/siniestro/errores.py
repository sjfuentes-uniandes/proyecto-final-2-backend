"""Errores de dominio del siniestro (HU-W19 y HU-W20). Ninguno lleva valores del caso: solo nombres
de campos y estados, para que la aplicación los traduzca a respuestas estables."""

from collections.abc import Sequence


class SiniestroInvalido(ValueError):
    """El siniestro o sus datos no cumplen una invariante; el mensaje nombra el campo, nunca su
    valor (puede traer datos del caso)."""


class TransicionInvalida(Exception):
    """El estado no admite el cambio pedido (DS3-02). `desde` es el estado vigente (el que se
    informa como `estadoActual`) y `hacia`, el estado al que la acción lo llevaría."""

    def __init__(self, desde: object, hacia: str) -> None:
        super().__init__(f"{desde} -> {hacia}")
        self.desde = desde
        self.hacia = hacia


class CasoAsignadoAOtro(Exception):
    """Solo el analista asignado decide (DS3-03)."""


class JustificacionRequerida(Exception):
    """La decisión no trae justificación (DS3-12): es obligatoria en las tres."""


class CausalInvalida(Exception):
    """La causal del rechazo no es de la lista cerrada (DS3-12)."""


class _ConCampos(Exception):
    def __init__(self, campos: Sequence[str]) -> None:
        super().__init__(", ".join(campos))
        self.campos = tuple(campos)


class ValorFueraDeRango(_ConCampos):
    """La liquidación incumple una regla de rango (DS3-12, R-67); `campos` los nombra."""


class DecisionInvalida(_ConCampos):
    """Un dato de la decisión falta o no es válido (texto al cliente vacío, evidencia ajena, …)."""
