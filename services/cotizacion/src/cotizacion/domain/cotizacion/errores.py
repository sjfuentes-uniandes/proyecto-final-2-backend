"""Errores de dominio de la cotización (HU-W03). La API los traduce a códigos estables.

Los errores técnicos (DependenciaNoDisponible) y de idempotencia son los del perfilamiento.
"""

from datetime import datetime
from uuid import UUID


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


class CursorInvalido(Exception):
    """El cursor del listado no lo entregó cotizacion (opaco para quien lo usa)."""


class RolNoPermitido(Exception):
    """El rol del actor no permite la operación (un socio no usa perfiles ni otro socioId)."""


class OfertaVencida(Exception):
    """La oferta ya no está vigente (ahora ≥ vigenteHasta): hay que cotizar de nuevo (HU-W04)."""

    def __init__(self, oferta_id: UUID, vigente_hasta: datetime) -> None:
        super().__init__(str(oferta_id))
        self.oferta_id = oferta_id
        self.vigente_hasta = vigente_hasta
