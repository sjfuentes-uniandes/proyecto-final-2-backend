from typing import Any
from uuid import UUID

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from bff_movil.application.registro.puertos import ServicioRegistro
from bff_movil.domain.registro.modelos import (
    ClienteVinculado,
    RegistroConCorreoDistinto,
    RegistroVerificado,
    RegistroYaVinculado,
    TokenRegistroInvalido,
)
from bff_movil.domain.sesion.errores import ServicioNoDisponible
from bff_movil.domain.verificacion.modelos import ResultadoKyc


def _resultado(error: ServicioNoDisponible) -> str:
    return RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR


class VerificarRegistro:
    def __init__(self, servicio: ServicioRegistro) -> None:
        self._servicio = servicio

    async def ejecutar(
        self, registro_id: UUID, correo: str, selfie: str, prueba_de_vida: dict[str, Any]
    ) -> RegistroVerificado:
        """Envía la evidencia a `clientes`. No la guarda ni la registra."""
        with operacion("registro.verificar") as op:
            try:
                registro = await self._servicio.verificar(
                    registro_id, correo, selfie, prueba_de_vida
                )
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except (RegistroConCorreoDistinto, RegistroYaVinculado):
                op.marcar(RESULTADO_RECHAZADO)
                raise
            if registro.resultado is not ResultadoKyc.APROBADO:
                op.marcar(RESULTADO_RECHAZADO)
        return registro


class VincularRegistro:
    def __init__(self, servicio: ServicioRegistro) -> None:
        self._servicio = servicio

    async def ejecutar(self, registro_id: UUID, sub: str, token_registro: str) -> ClienteVinculado:
        with operacion("registro.vincular") as op:
            try:
                return await self._servicio.vincular(registro_id, sub, token_registro)
            except ServicioNoDisponible as error:
                op.marcar(_resultado(error))
                raise
            except (TokenRegistroInvalido, RegistroYaVinculado):
                op.marcar(RESULTADO_RECHAZADO)
                raise
