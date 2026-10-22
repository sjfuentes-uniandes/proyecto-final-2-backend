"""Lecturas de la cotización (HU-W03): una cotización por id y las condiciones del producto.

- Un socio solo lee sus cotizaciones; la de otro socio responde como inexistente, para no revelar
  que existe (como CONSENTIMIENTO_NO_ENCONTRADO). Un asesor lee cualquiera.
- Las condiciones son lo que el portal necesita para el formulario (mockups 1 y 2): coberturas,
  versión del producto y de las reglas, y límites. Aplican la misma tabla de decisión que el cálculo.
"""

from dataclasses import dataclass
from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from cotizacion.application.cotizacion.puertos import PuertoProductos, RepositorioCotizaciones
from cotizacion.application.perfilamiento.puertos import Reloj
from cotizacion.domain.cotizacion.elegibilidad import verificar_elegibilidad
from cotizacion.domain.cotizacion.errores import (
    CotizacionNoEncontrada,
    ProductoFueraDeVigencia,
    ProductoInactivo,
    ProductoNoEncontrado,
    ReglasNoVigentes,
)
from cotizacion.domain.cotizacion.modelos import Actor, Cotizacion, Producto, ReglasPrima, RolActor


def puede_leer(actor: Actor, cotizacion: Cotizacion) -> bool:
    return actor.rol is RolActor.ASESOR or cotizacion.socio_id == actor.id


class ConsultarCotizacion:
    def __init__(self, repo: RepositorioCotizaciones) -> None:
        self._repo = repo

    async def ejecutar(self, cotizacion_id: UUID, actor: Actor) -> Cotizacion:
        with operacion("cotizacion.consultar") as op:
            cotizacion = await self._repo.por_id(cotizacion_id)
            if cotizacion is None or not puede_leer(actor, cotizacion):
                op.marcar(RESULTADO_RECHAZADO)
                raise CotizacionNoEncontrada()
        return cotizacion


@dataclass(frozen=True, slots=True)
class Condiciones:
    producto: Producto
    reglas: ReglasPrima


class ConsultarCondiciones:
    def __init__(self, productos: PuertoProductos, reloj: Reloj) -> None:
        self._productos = productos
        self._reloj = reloj

    async def ejecutar(self, producto_id: str) -> Condiciones:
        with operacion("cotizacion.condiciones") as op:
            ahora = self._reloj.ahora()
            try:
                producto = (await self._productos.producto(producto_id)).valor
                reglas = (await self._productos.reglas_vigentes(producto_id, ahora)).valor
                verificar_elegibilidad(producto, reglas, ahora)
            except (ProductoNoEncontrado, ProductoInactivo, ProductoFueraDeVigencia):
                op.marcar(RESULTADO_RECHAZADO)
                raise
            except ReglasNoVigentes:
                op.marcar(RESULTADO_RECHAZADO)
                # Sin reglas, el producto decide antes (tabla de decisión: filas 2 y 3).
                verificar_elegibilidad(producto, None, ahora)
                raise
        return Condiciones(producto, reglas)
