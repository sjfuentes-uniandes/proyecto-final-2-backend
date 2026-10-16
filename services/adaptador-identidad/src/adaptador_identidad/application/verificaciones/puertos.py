"""Puerto de salida hacia el proveedor KYC."""

from typing import Any, Protocol
from uuid import UUID

from adaptador_identidad.domain.verificaciones.modelos import Verificacion


class PuertoProveedorKyc(Protocol):
    async def verificar(
        self,
        cliente_id: UUID,
        selfie: str,
        prueba_de_vida: dict[str, Any],
        timeout_s: float,
    ) -> Verificacion: ...
