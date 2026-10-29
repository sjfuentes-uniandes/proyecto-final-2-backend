"""Puerto de salida hacia el proveedor de pagos."""

from typing import Protocol
from uuid import UUID

from adaptador_pagos.domain.cobros.modelos import Cobro, SolicitudCobro


class PuertoProveedorPagos(Protocol):
    async def cobrar(self, solicitud: SolicitudCobro, timeout_s: float) -> Cobro: ...

    async def consultar(
        self, clave_recaudo: UUID, pagador_ref: UUID, timeout_s: float
    ) -> Cobro: ...
