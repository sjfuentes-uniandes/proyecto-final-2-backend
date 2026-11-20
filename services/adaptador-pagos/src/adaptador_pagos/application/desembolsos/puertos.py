"""Puerto de salida hacia el proveedor de pagos para los desembolsos."""

from typing import Protocol

from adaptador_pagos.domain.desembolsos.modelos import Desembolso, SolicitudDesembolso


class PuertoProveedorDesembolsos(Protocol):
    async def desembolsar(self, solicitud: SolicitudDesembolso, timeout_s: float) -> Desembolso: ...

    async def consultar(
        self, referencia: str, beneficiario_ref: str, timeout_s: float
    ) -> Desembolso: ...
