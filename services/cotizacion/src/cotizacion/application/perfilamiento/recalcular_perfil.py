"""Recálculo de un perfil (W12-P06, DT-25 y DT-26).

Crea un perfil nuevo e inmutable a partir de uno anterior, con el modelo vigente y verificando
de nuevo la identidad y el consentimiento. El perfil anterior no cambia. La idempotencia por
`Idempotency-Key` la resuelve CrearPerfil: la huella incluye `perfilAnteriorId`.
"""

from uuid import UUID

from cotizacion.application.perfilamiento.crear_perfil import ComandoPerfil, CrearPerfil
from cotizacion.application.perfilamiento.puertos import RepositorioPerfiles
from cotizacion.domain.perfilamiento.errores import PerfilNoEncontrado
from cotizacion.domain.perfilamiento.modelos import Perfil

OPERACION_RECALCULO = "perfilamiento.recalculo"


class RecalcularPerfil:
    def __init__(self, crear_perfil: CrearPerfil, perfiles: RepositorioPerfiles) -> None:
        self._crear_perfil = crear_perfil
        self._perfiles = perfiles

    async def ejecutar(
        self, perfil_id: UUID, idempotency_key: UUID, actor_id: str, correlation_id: str
    ) -> tuple[Perfil, bool]:
        """(perfil nuevo, creado). `creado` es falso si la clave ya se había usado."""
        anterior = await self._perfiles.por_id(perfil_id)
        if anterior is None:
            raise PerfilNoEncontrado()
        return await self._crear_perfil.ejecutar(
            ComandoPerfil(
                cliente_id=anterior.cliente_id,
                producto_id=anterior.producto_id,
                datos_declarados=anterior.datos_declarados,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                correlation_id=correlation_id,
                perfil_anterior_id=anterior.id,
                operacion=OPERACION_RECALCULO,
            )
        )
