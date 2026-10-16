from typing import Any, Protocol
from uuid import UUID

from bff_movil.domain.registro.modelos import ClienteVinculado, RegistroVerificado


class ServicioRegistro(Protocol):
    async def verificar(
        self, registro_id: UUID, correo: str, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> RegistroVerificado:
        """Lanza RegistroConCorreoDistinto, RegistroYaVinculado o ServicioNoDisponible."""
        ...

    async def vincular(self, registro_id: UUID, sub: str, token_registro: str) -> ClienteVinculado:
        """Lanza TokenRegistroInvalido, RegistroYaVinculado o ServicioNoDisponible."""
        ...
