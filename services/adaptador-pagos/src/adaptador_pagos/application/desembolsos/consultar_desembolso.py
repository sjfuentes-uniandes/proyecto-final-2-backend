"""Caso de uso: consultar el estado de un desembolso para conciliar (ADR 0015, DS3-18)."""

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from adaptador_pagos.application.desembolsos.puertos import PuertoProveedorDesembolsos
from adaptador_pagos.domain.cobros.modelos import RespuestaInvalida
from adaptador_pagos.domain.desembolsos.modelos import Desembolso, DesembolsoNoEncontrado


class ConsultarDesembolso:
    def __init__(
        self, proveedor: PuertoProveedorDesembolsos, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(self, referencia: str, beneficiario_ref: str) -> Desembolso:
        async def llamada(timeout_s: float) -> Desembolso:
            return await self._proveedor.consultar(referencia, beneficiario_ref, timeout_s)

        with operacion("desembolso.consultar") as op:
            try:
                return await self._proteccion.ejecutar(
                    clave="pagos",
                    grupo="pagos",
                    llamada=llamada,
                    configurado_ms=self._timeout_ms,
                    deadline_ms=None,
                )
            except DesembolsoNoEncontrado:
                # Respuesta definitiva del proveedor, no una falla.
                op.marcar(RESULTADO_RECHAZADO)
                raise
            except TiempoAgotado:
                op.marcar(RESULTADO_TIMEOUT)
                raise
            except (DependenciaNoDisponible, RespuestaInvalida):
                op.marcar(RESULTADO_ERROR)
                raise
