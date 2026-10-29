"""Errores de negocio de las reglas de fallback; la API los traduce a B2 (HU-W06)."""


class ReglasFallbackNoVigentes(Exception):
    """El producto no tiene reglas de fallback vigentes en la fecha, o no existe esa versión."""


class ReglasFallbackInvalidas(Exception):
    """Una semilla de reglas de fallback no cumple sus validaciones: catalogo no arranca."""

    def __init__(self, errores: tuple[str, ...]) -> None:
        super().__init__("reglas de fallback inválidas: " + ", ".join(errores))
        self.errores = errores
