from typing import Any, Protocol
from uuid import UUID

from bff_movil.domain.polizas.modelos import Poliza


class SiniestrosApi(Protocol):
    async def limites(self, actor_id: str) -> dict[str, Any]: ...

    async def crear_borrador(
        self, actor_id: str, cliente_id: UUID, poliza: Poliza, descripcion: str, clave: UUID
    ) -> dict[str, Any]: ...

    async def agregar_evidencia(
        self, actor_id: str, borrador_id: UUID, solicitud: dict[str, Any]
    ) -> dict[str, Any]: ...
