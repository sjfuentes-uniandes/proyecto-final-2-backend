from collections.abc import Mapping
from uuid import UUID

from bff_movil.domain.sesion.errores import ClienteNoEncontrado


class ResolutorClienteEnMemoria:
    """ResolutorCliente con una relación fija sub -> clienteId.

    TODO: reemplazarlo por el adaptador real cuando se defina cómo se relaciona el
    `sub` de Cognito con el clienteId (otra historia). Sin relaciones, todo titular responde
    como no encontrado.
    """

    def __init__(self, relaciones: Mapping[str, UUID] | None = None) -> None:
        self._relaciones = dict(relaciones or {})

    async def resolver(self, sub: str) -> UUID:
        try:
            return self._relaciones[sub]
        except KeyError as error:
            raise ClienteNoEncontrado() from error
