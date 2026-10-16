from typing import Any, Protocol

from bff_movil.domain.verificacion.modelos import Verificacion


class ServicioVerificacion(Protocol):
    async def verificar(
        self, sub: str, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> Verificacion:
        """Lanza ServicioNoDisponible si la verificación no terminó (recuperable)."""
        ...
