"""Errores de dominio del movimiento de recaudo (HU-W16)."""


class RecaudoInvalido(ValueError):
    """El movimiento no cumple una invariante; el mensaje nombra el campo, nunca su valor."""


class TransicionInvalida(Exception):
    """Se intentó salir de un estado final (CONFIRMADO o RECHAZADO; plan §d)."""

    def __init__(self, desde: object, hacia: str) -> None:
        super().__init__(f"{desde} -> {hacia}")
        self.desde = desde
        self.hacia = hacia
