"""Caso de uso ConsultarAceptacion (HU-W15, W15-B03): la aceptación de una cotización."""

from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from polizas.application.aceptacion.puertos import AceptacionesRepositorio
from polizas.domain.aceptacion.errores import AceptacionNoEncontrada
from polizas.domain.aceptacion.modelos import Aceptacion

OPERACION = "aceptacion.consultar"


class ConsultarAceptacion:
    def __init__(self, aceptaciones: AceptacionesRepositorio) -> None:
        self._aceptaciones = aceptaciones

    async def por_cotizacion(self, cotizacion_id: UUID) -> Aceptacion:
        """Lanza AceptacionNoEncontrada si la cotización no tiene aceptación."""
        with operacion(OPERACION) as op:
            aceptacion = await self._aceptaciones.por_cotizacion(cotizacion_id)
            if aceptacion is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise AceptacionNoEncontrada()
            return aceptacion
