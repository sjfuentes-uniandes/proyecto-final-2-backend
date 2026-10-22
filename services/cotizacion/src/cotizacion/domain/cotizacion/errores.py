"""Errores de dominio de la cotización (HU-W03). La API los traduce a códigos estables.

Los errores técnicos (DependenciaNoDisponible) y de idempotencia son los del perfilamiento.
"""


class ProductoNoEncontrado(Exception):
    """No existe un producto con ese identificador."""


class ProductoInactivo(Exception):
    """El producto existe pero no está ACTIVO."""


class ProductoFueraDeVigencia(Exception):
    """La fecha de la cotización no cae en [vigenteDesde, vigenteHasta) del producto."""


class ReglasNoVigentes(Exception):
    """El producto no tiene reglas de prima vigentes en la fecha de la cotización."""


class DatosFueraDeRango(Exception):
    """Los datos del asegurado están fuera de los límites de las reglas del producto."""

    def __init__(self, campos: tuple[str, ...]) -> None:
        super().__init__(", ".join(campos))
        self.campos = campos


class CoberturaNoDisponible(Exception):
    """Una cobertura opcional pedida no es opcional en el producto."""

    def __init__(self, campos: tuple[str, ...]) -> None:
        super().__init__(", ".join(campos))
        self.campos = campos


class PerfilNoCorresponde(Exception):
    """El perfil es de otro cliente u otro producto, o se calculó con otros datos declarados."""

    def __init__(self, campos: tuple[str, ...]) -> None:
        super().__init__(", ".join(campos))
        self.campos = campos


class CotizacionNoEncontrada(Exception):
    """La cotización no existe o es de otro socio: no se distingue (no revela su existencia)."""


class RolNoPermitido(Exception):
    """El rol del actor no permite la operación (un socio no usa perfiles ni otro socioId)."""
