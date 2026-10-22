"""Errores de negocio de las reglas de suscripción; la API los traduce a B2 (HU-W13)."""


class ReglasSuscripcionNoVigentes(Exception):
    """El producto no tiene reglas de suscripción vigentes en la fecha, o no existe esa versión."""


class ReglasSuscripcionInvalidas(Exception):
    """Una semilla de reglas de suscripción no cumple sus validaciones: catalogo no arranca."""

    def __init__(self, errores: tuple[str, ...]) -> None:
        super().__init__("reglas de suscripción inválidas: " + ", ".join(errores))
        self.errores = errores
