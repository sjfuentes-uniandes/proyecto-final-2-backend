from typing import Any

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from bff_movil.application.verificacion.puertos import ServicioVerificacion
from bff_movil.domain.sesion.errores import ServicioNoDisponible
from bff_movil.domain.verificacion.modelos import ResultadoKyc, Verificacion


class VerificarIdentidad:
    def __init__(self, servicio: ServicioVerificacion) -> None:
        self._servicio = servicio

    async def ejecutar(self, sub: str, selfie: str, prueba_de_vida: dict[str, Any]) -> Verificacion:
        """Envía la evidencia del titular del token a `clientes`. No la guarda ni la registra."""
        with operacion("verificacion.enviar") as op:
            try:
                verificacion = await self._servicio.verificar(sub, selfie, prueba_de_vida)
            except ServicioNoDisponible as error:
                op.marcar(
                    RESULTADO_TIMEOUT if error.motivo == "TIEMPO_AGOTADO" else RESULTADO_ERROR
                )
                raise
            if verificacion.resultado is not ResultadoKyc.APROBADO:
                op.marcar(RESULTADO_RECHAZADO)
        return verificacion
