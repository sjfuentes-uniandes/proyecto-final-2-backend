"""Puerto de salida hacia el proveedor KYC."""

from typing import Any, Protocol
from uuid import UUID

from adaptador_identidad.domain.verificaciones.modelos import (
    DatosDocumento,
    Verificacion,
    VerificacionDocumento,
)


class PuertoProveedorKyc(Protocol):
    async def verificar(
        self,
        cliente_id: UUID,
        selfie: str,
        prueba_de_vida: dict[str, Any],
        timeout_s: float,
    ) -> Verificacion: ...


class PuertoProveedorDocumental(Protocol):
    async def verificar_documento(
        self, cliente_id: UUID, datos: DatosDocumento, timeout_s: float
    ) -> VerificacionDocumento: ...
