"""Errores de dominio de la póliza (HU-W17, 17.6). Los mensajes nombran el campo, nunca su valor."""


class PolizaInvalida(ValueError):
    """La póliza, su documento o su firma no cumplen una invariante."""


class TransicionInvalida(Exception):
    """La transición no se permite desde el estado actual: un punto de control ya registrado, uno
    que falta o cualquier intento sobre una póliza ACTIVA (plan §d)."""

    def __init__(self, desde: object, hacia: str) -> None:
        super().__init__(f"{desde} -> {hacia}")
        self.desde = desde
        self.hacia = hacia


class ProductoSinRamo(Exception):
    """El producto no está en la tabla de ramos de polizas (ADR 0014, decisión 2): sin ramo no hay
    número."""

    codigo = "PRODUCTO_SIN_RAMO"

    def __init__(self, producto_id: str) -> None:
        super().__init__(self.codigo)
        self.producto_id = producto_id


class HuellaNoCoincide(Exception):
    """La huella firmada no es la del documento registrado."""

    codigo = "HUELLA_NO_COINCIDE"

    def __init__(self) -> None:
        super().__init__(self.codigo)
