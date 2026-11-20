"""Caso de uso: ordenar el desembolso de una indemnización con el proveedor de pagos (HU-W23)."""

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from adaptador_pagos.application.desembolsos.puertos import PuertoProveedorDesembolsos
from adaptador_pagos.domain.cobros.modelos import RespuestaInvalida
from adaptador_pagos.domain.desembolsos.modelos import (
    Desembolso,
    EstadoDesembolso,
    SolicitudDesembolso,
)


class Desembolsar:
    """Una sola llamada, sin reintentos: la incertidumbre la resuelven `dinero` y su conciliación."""

    def __init__(
        self, proveedor: PuertoProveedorDesembolsos, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(self, solicitud: SolicitudDesembolso) -> Desembolso:
        async def llamada(timeout_s: float) -> Desembolso:
            return await self._proveedor.desembolsar(solicitud, timeout_s)

        with operacion("desembolso.ordenar") as op:
            try:
                desembolso = await self._proteccion.ejecutar(
                    clave="pagos",
                    grupo="pagos",
                    llamada=llamada,
                    configurado_ms=self._timeout_ms,
                    deadline_ms=None,
                )
            except TiempoAgotado:
                op.marcar(RESULTADO_TIMEOUT)
                raise
            except (DependenciaNoDisponible, RespuestaInvalida):
                op.marcar(RESULTADO_ERROR)
                raise
            if desembolso.estado is EstadoDesembolso.RECHAZADO:
                op.marcar(RESULTADO_RECHAZADO)
            return desembolso
