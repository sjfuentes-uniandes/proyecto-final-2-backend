"""Errores de dominio de la indemnización (HU-W23)."""


class IndemnizacionInvalida(ValueError):
    """La indemnización no cumple una invariante; el mensaje nombra el campo, nunca su valor."""


class TransicionInvalida(Exception):
    """Se intentó salir de un estado final (CONFIRMADA o RECHAZADA)."""

    def __init__(self, desde: object, hacia: str) -> None:
        super().__init__(f"{desde} -> {hacia}")
        self.desde = desde
        self.hacia = hacia
