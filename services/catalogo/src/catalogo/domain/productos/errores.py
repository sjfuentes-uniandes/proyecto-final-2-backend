"""Errores de negocio de productos y reglas de prima; la API los traduce a B2."""


class ProductoNoEncontrado(Exception):
    """No existe un producto con ese identificador."""


class ReglasNoVigentes(Exception):
    """El producto no tiene reglas de prima vigentes en la fecha, o no existe esa versión."""


class ReglasInvalidas(Exception):
    """Una semilla de reglas de prima no cumple sus validaciones: catalogo no arranca."""

    def __init__(self, errores: tuple[str, ...]) -> None:
        super().__init__("reglas de prima inválidas: " + ", ".join(errores))
        self.errores = errores
