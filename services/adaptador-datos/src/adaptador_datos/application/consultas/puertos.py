"""Puertos del adaptador de datos (02, CS-06)."""

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from adaptador_datos.domain.consultas.modelos import Fuente


class PuertoProveedor(Protocol):
    async def obtener(
        self, fuente: Fuente, cliente_id: UUID, campos_proveedor: tuple[str, ...], timeout_s: float
    ) -> Mapping[str, Any]:
        """Cuerpo del proveedor; lanza httpx.TimeoutException, httpx.HTTPError o HTTPStatusError (5xx)."""
        ...
