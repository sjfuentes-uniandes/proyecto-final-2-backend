"""Errores de dominio del siniestro (HU-W19)."""


class SiniestroInvalido(ValueError):
    """El siniestro o sus datos no cumplen una invariante; el mensaje nombra el campo, nunca su
    valor (puede traer datos del caso)."""


class TransicionInvalida(Exception):
    """El estado no admite el cambio pedido (DS3-02). W19 solo ejecuta REPORTADO → EN_EVALUACION."""

    def __init__(self, desde: object, hacia: str) -> None:
        super().__init__(f"{desde} -> {hacia}")
        self.desde = desde
        self.hacia = hacia
