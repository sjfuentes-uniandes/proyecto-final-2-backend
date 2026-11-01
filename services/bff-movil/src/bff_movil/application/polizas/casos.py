from uuid import UUID

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from bff_movil.application.polizas.puertos import ConsultaPolizas
from bff_movil.application.sesion.puertos import ResolutorCliente
from bff_movil.domain.polizas.modelos import Poliza, PolizaNoAutorizada, PolizaNoEncontrada
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

_RECHAZOS = (ClienteNoEncontrado, PolizaNoAutorizada, PolizaNoEncontrada)


def _resultado(error: ServicioNoDisponible) -> str:
    return RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR


class ListarPolizas:
    def __init__(self, resolutor: ResolutorCliente, polizas: ConsultaPolizas) -> None:
        self._resolutor = resolutor
        self._polizas = polizas

    async def ejecutar(self, sub: str) -> list[Poliza]:
        """El clienteId sale del `sub` del token, nunca de la app."""
        with operacion("polizas.listar") as op:
            try:
                cliente_id = await self._resolutor.resolver(sub)
                return await self._polizas.listar(cliente_id, sub)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except ClienteNoEncontrado:
                op.marcar(RESULTADO_RECHAZADO)
                raise


class ConsultarPoliza:
    def __init__(self, resolutor: ResolutorCliente, polizas: ConsultaPolizas) -> None:
        self._resolutor = resolutor
        self._polizas = polizas

    async def ejecutar(self, sub: str, poliza_id: UUID) -> Poliza:
        with operacion("polizas.consultar") as op:
            try:
                cliente_id = await self._resolutor.resolver(sub)
                return await self._polizas.obtener(cliente_id, poliza_id, sub)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except _RECHAZOS:
                op.marcar(RESULTADO_RECHAZADO)
                raise
