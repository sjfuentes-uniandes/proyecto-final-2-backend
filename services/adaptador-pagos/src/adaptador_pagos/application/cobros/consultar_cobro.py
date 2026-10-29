"""Caso de uso: consultar el estado de un cobro para conciliar (ADR 0013, decisión 5)."""

from uuid import UUID

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from adaptador_pagos.application.cobros.puertos import PuertoProveedorPagos
from adaptador_pagos.domain.cobros.modelos import Cobro, CobroNoEncontrado, RespuestaInvalida


class ConsultarCobro:
    def __init__(
        self, proveedor: PuertoProveedorPagos, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(self, clave_recaudo: UUID, pagador_ref: UUID) -> Cobro:
        async def llamada(timeout_s: float) -> Cobro:
            return await self._proveedor.consultar(clave_recaudo, pagador_ref, timeout_s)

        with operacion("conciliacion.pagos") as op:
            try:
                return await self._proteccion.ejecutar(
                    clave="pagos",
                    grupo="pagos",
                    llamada=llamada,
                    configurado_ms=self._timeout_ms,
                    deadline_ms=None,
                )
            except CobroNoEncontrado:
                # Respuesta definitiva del proveedor, no una falla.
                op.marcar(RESULTADO_RECHAZADO)
                raise
            except TiempoAgotado:
                op.marcar(RESULTADO_TIMEOUT)
                raise
            except (DependenciaNoDisponible, RespuestaInvalida):
                op.marcar(RESULTADO_ERROR)
                raise
