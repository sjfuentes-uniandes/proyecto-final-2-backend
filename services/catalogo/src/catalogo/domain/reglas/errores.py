"""Errores de negocio del modelo de riesgo; la API los traduce a B2 (04 §B.4.1)."""


class VersionExistente(Exception):
    """La versión ya está publicada (los modelos son inmutables)."""


class DefinicionInvalida(Exception):
    def __init__(self, errores: tuple[str, ...]) -> None:
        super().__init__("definición inválida")
        self.errores = errores


class VigenciaRetroactiva(Exception):
    """vigenteDesde es anterior a ahora − 5 s."""


class ModeloNoEncontrado(Exception):
    """No hay modelo para la versión o no hay uno vigente para el producto en la fecha."""
