"""Caso de uso: cobrar el primer recaudo de la prima con el proveedor de pagos (HU-W16)."""

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from adaptador_pagos.application.cobros.puertos import PuertoProveedorPagos
from adaptador_pagos.domain.cobros.modelos import (
    Cobro,
    EstadoCobro,
    RespuestaInvalida,
    SolicitudCobro,
)


class Cobrar:
    """Una sola llamada, sin reintentos: la incertidumbre la resuelven `dinero` y la conciliación."""

    def __init__(
        self, proveedor: PuertoProveedorPagos, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(self, solicitud: SolicitudCobro) -> Cobro:
        async def llamada(timeout_s: float) -> Cobro:
            return await self._proveedor.cobrar(solicitud, timeout_s)

        # `cobro.pagos` es la operación de la alarma de degradación del proveedor (infra).
        with operacion("cobro.pagos") as op:
            try:
                cobro = await self._proteccion.ejecutar(
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
            if cobro.estado is EstadoCobro.RECHAZADO:
                op.marcar(RESULTADO_RECHAZADO)
            return cobro
