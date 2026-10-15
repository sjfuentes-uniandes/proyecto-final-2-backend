from datetime import datetime

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from bff_movil.application.sesion.puertos import ConsultaIdentidad, ResolutorCliente
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible
from bff_movil.domain.sesion.modelos import Sesion, servicios_autorizados


class ConsultarSesion:
    def __init__(self, resolutor: ResolutorCliente, identidad: ConsultaIdentidad) -> None:
        self._resolutor = resolutor
        self._identidad = identidad

    async def ejecutar(self, sub: str, expira_en: datetime) -> Sesion:
        """Compone la sesión del titular del token con su estado de identidad."""
        cliente_id = await self._resolutor.resolver(sub)
        with operacion("sesion.consultar_identidad") as op:
            try:
                estado = await self._identidad.obtener(cliente_id, sub)
            except ClienteNoEncontrado:
                op.marcar(RESULTADO_RECHAZADO)
                raise
            except ServicioNoDisponible as error:
                op.marcar(
                    RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR
                )
                raise
        return Sesion(
            cliente_id=cliente_id,
            servicios_autorizados=servicios_autorizados(estado),
            expira_en=expira_en,
        )
