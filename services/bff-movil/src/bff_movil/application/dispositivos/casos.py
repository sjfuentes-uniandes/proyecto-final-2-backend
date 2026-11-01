from uuid import UUID

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from bff_movil.application.dispositivos.puertos import GestionDispositivos
from bff_movil.application.sesion.puertos import ResolutorCliente
from bff_movil.domain.dispositivos.modelos import DispositivoNoAutorizado, DispositivoNoEncontrado
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

_RECHAZOS = (ClienteNoEncontrado, DispositivoNoAutorizado, DispositivoNoEncontrado)


def _resultado(error: ServicioNoDisponible) -> str:
    return RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR


class RegistrarDispositivo:
    def __init__(self, resolutor: ResolutorCliente, dispositivos: GestionDispositivos) -> None:
        self._resolutor = resolutor
        self._dispositivos = dispositivos

    async def ejecutar(self, sub: str, token: str) -> UUID:
        """El clienteId sale del `sub` del token, nunca de la app. El token no se registra."""
        with operacion("dispositivos.registrar") as op:
            try:
                cliente_id = await self._resolutor.resolver(sub)
                return await self._dispositivos.registrar(cliente_id, token, sub)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except ClienteNoEncontrado:
                op.marcar(RESULTADO_RECHAZADO)
                raise


class DarDeBajaDispositivo:
    def __init__(self, resolutor: ResolutorCliente, dispositivos: GestionDispositivos) -> None:
        self._resolutor = resolutor
        self._dispositivos = dispositivos

    async def ejecutar(self, sub: str, dispositivo_id: UUID) -> None:
        with operacion("dispositivos.dar_de_baja") as op:
            try:
                cliente_id = await self._resolutor.resolver(sub)
                await self._dispositivos.dar_de_baja(cliente_id, dispositivo_id, sub)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except _RECHAZOS:
                op.marcar(RESULTADO_RECHAZADO)
                raise
