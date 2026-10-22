"""Consulta de las reglas de suscripción vigentes o de una versión (HU-W13).

Las consume `polizas`: las vigentes para decidir y una versión cerrada para reconstruir.
"""

from datetime import datetime

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from catalogo.application.productos.puertos import RepositorioProductos
from catalogo.application.reglas.puertos import Reloj
from catalogo.application.suscripcion.puertos import RepositorioReglasSuscripcion
from catalogo.domain.productos.errores import ProductoNoEncontrado
from catalogo.domain.suscripcion.errores import ReglasSuscripcionNoVigentes
from catalogo.domain.suscripcion.reglas import ReglasSuscripcion


class ConsultarReglasSuscripcion:
    def __init__(
        self, repo: RepositorioReglasSuscripcion, productos: RepositorioProductos, reloj: Reloj
    ) -> None:
        self._repo = repo
        self._productos = productos
        self._reloj = reloj

    async def vigentes(self, producto_id: str, en: datetime | None) -> ReglasSuscripcion:
        with operacion("reglas_suscripcion.consultar") as op:
            reglas = await self._repo.vigentes(producto_id, en or self._reloj.ahora())
            return await self._encontradas(producto_id, reglas, op)

    async def por_version(self, producto_id: str, version: str) -> ReglasSuscripcion:
        with operacion("reglas_suscripcion.consultar") as op:
            reglas = await self._repo.por_version(producto_id, version)
            return await self._encontradas(producto_id, reglas, op)

    async def _encontradas(
        self, producto_id: str, reglas: ReglasSuscripcion | None, op: operacion
    ) -> ReglasSuscripcion:
        """Sin reglas, distingue el producto inexistente de las reglas que faltan."""
        if reglas is not None:
            return reglas
        op.marcar(RESULTADO_RECHAZADO)
        if await self._productos.producto(producto_id) is None:
            raise ProductoNoEncontrado()
        raise ReglasSuscripcionNoVigentes()
