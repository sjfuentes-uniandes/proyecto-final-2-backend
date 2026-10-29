"""Caso de uso ConsultarRecaudo (HU-W16, 16.13): el estado del recaudo de una cotización.

Lee la aceptación, su decisión (la prima de la instantánea) y la proyección por `claveRecaudo`
(el id de la aceptación). Sin llamadas a otros servicios.
"""

from uuid import UUID

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from polizas.application.aceptacion.puertos import AceptacionesRepositorio
from polizas.application.recaudo.puertos import RecaudosRepositorio
from polizas.application.suscripcion.puertos import DecisionesRepositorio
from polizas.domain.aceptacion.errores import AceptacionNoEncontrada
from polizas.domain.recaudo.modelos import EstadoRecaudo, estado_recaudo
from polizas.domain.suscripcion.errores import DependenciaNoDisponible

OPERACION = "recaudo.consultar"


class ConsultarRecaudo:
    def __init__(
        self,
        aceptaciones: AceptacionesRepositorio,
        decisiones: DecisionesRepositorio,
        recaudos: RecaudosRepositorio,
    ) -> None:
        self._aceptaciones = aceptaciones
        self._decisiones = decisiones
        self._recaudos = recaudos

    async def por_cotizacion(self, cotizacion_id: UUID) -> EstadoRecaudo:
        """Lanza AceptacionNoEncontrada si la cotización no tiene aceptación."""
        with operacion(OPERACION) as op:
            aceptacion = await self._aceptaciones.por_cotizacion(cotizacion_id)
            if aceptacion is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise AceptacionNoEncontrada()
            decision = await self._decisiones.por_id(aceptacion.decision_id)
            if decision is None:
                # No pasa: la aceptación tiene llave hacia su decisión y las dos son inmutables.
                raise DependenciaNoDisponible("polizas.decisiones")
            resultado = await self._recaudos.por_clave(aceptacion.id)
            return estado_recaudo(aceptacion, decision, resultado)
