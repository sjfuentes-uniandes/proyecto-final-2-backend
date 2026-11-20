from typing import Any, Protocol
from uuid import UUID

from bff_movil.domain.polizas.modelos import Poliza


class SiniestrosApi(Protocol):
    async def enlace_evidencia(
        self, actor_id: str, cliente_id: UUID, siniestro_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]: ...

    async def actualizar_borrador(
        self, actor_id: str, borrador_id: UUID, cambios: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def enviar_borrador(
        self, actor_id: str, borrador_id: UUID, clave: UUID, poliza: Poliza | None
    ) -> dict[str, Any]: ...

    async def eliminar_evidencia(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> None: ...

    async def listar_siniestros(
        self, actor_id: str, cliente_id: UUID, limite: int, cursor: str | None
    ) -> dict[str, Any]: ...

    async def consultar_siniestro(
        self, actor_id: str, cliente_id: UUID, siniestro_id: UUID
    ) -> dict[str, Any]: ...

    async def limites(self, actor_id: str) -> dict[str, Any]: ...

    async def listar_borradores(self, actor_id: str) -> dict[str, Any]: ...

    async def consultar_borrador(self, actor_id: str, borrador_id: UUID) -> dict[str, Any]: ...

    async def crear_borrador(
        self, actor_id: str, cliente_id: UUID, poliza: Poliza, descripcion: str, clave: UUID
    ) -> dict[str, Any]: ...

    async def agregar_evidencia(
        self, actor_id: str, borrador_id: UUID, solicitud: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def confirmar_evidencia(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]: ...

    async def renovar_carga(
        self, actor_id: str, borrador_id: UUID, evidencia_id: UUID
    ) -> dict[str, Any]: ...
