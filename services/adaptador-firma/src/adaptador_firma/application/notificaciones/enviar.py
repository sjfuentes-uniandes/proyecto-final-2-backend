"""Caso de uso: enviar un aviso push al proveedor (HU-M11, `notificacion.push`)."""

from solventa_common.resiliencia import DependenciaNoDisponible, Proteccion, TiempoAgotado
from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from adaptador_firma.application.notificaciones.puertos import PuertoProveedorPush
from adaptador_firma.domain.notificaciones.modelos import (
    Push,
    RespuestaInvalida,
    ResultadoPush,
    SolicitudPush,
)


class EnviarPush:
    """Una sola llamada, sin reintentos: `avisos` reintenta con su propio barrido."""

    def __init__(
        self, proveedor: PuertoProveedorPush, proteccion: Proteccion, timeout_ms: int
    ) -> None:
        self._proveedor = proveedor
        self._proteccion = proteccion
        self._timeout_ms = timeout_ms

    async def ejecutar(self, solicitud: SolicitudPush) -> Push:
        async def llamada(timeout_s: float) -> Push:
            return await self._proveedor.enviar(solicitud, timeout_s)

        # `notificacion.push` es la operación de la alarma de degradación del proveedor (infra).
        with operacion("notificacion.push") as op:
            try:
                push = await self._proteccion.ejecutar(
                    clave="push",
                    grupo="push",
                    llamada=llamada,
                    configurado_ms=self._timeout_ms,
                    deadline_ms=None,
                )
            except TiempoAgotado:
                op.marcar(RESULTADO_TIMEOUT)
                raise
            except (DependenciaNoDisponible, RespuestaInvalida):
                op.marcar(RESULTADO_ERROR)
                raise
            # Un token no reconocido no es un fallo del proveedor: no cuenta en OperationErrors.
            if push.resultado is ResultadoPush.RECHAZADO:
                op.marcar(RESULTADO_RECHAZADO)
            return push
