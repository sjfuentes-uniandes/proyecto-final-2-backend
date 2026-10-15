"""Ejecutor de las consultas a las fuentes autorizadas (fase B de crear_perfil)."""

import asyncio
import logging
from collections.abc import Sequence
from time import perf_counter

from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_OK, RESULTADO_TIMEOUT, operacion

from cotizacion.application.perfilamiento.puertos import PuertoFuente
from cotizacion.domain.perfilamiento.modelos import (
    ContextoConsulta,
    EstadoFuente,
    ResultadoFuente,
    SolicitudFuente,
)

logger = logging.getLogger("cotizacion.perfilamiento")

_RESULTADO_RAMA = {
    EstadoFuente.CONSULTADA: RESULTADO_OK,
    EstadoFuente.TIEMPO_AGOTADO: RESULTADO_TIMEOUT,
}


class EjecutorParalelo:
    """Una rama por fuente, todas a la vez, cada una con su propio timeout (HU-W12, DT-10).

    - La rama que excede `fuente_timeout_ms` se cancela y queda TIEMPO_AGOTADO.
    - Ninguna excepción se propaga entre ramas: una falla deja la fuente NO_DISPONIBLE.
    - Solo se propaga asyncio.CancelledError, que es la cancelación del recorrido completo.
    - Cada rama se mide con `operacion("perfilamiento.dependencia.fuente.<FUENTE>")` (DT-11).
    """

    def __init__(self, puerto: PuertoFuente, fuente_timeout_ms: int) -> None:
        self._puerto = puerto
        self._timeout_s = fuente_timeout_ms / 1000

    async def ejecutar(
        self, solicitudes: Sequence[SolicitudFuente], contexto: ContextoConsulta
    ) -> list[ResultadoFuente]:
        # gather conserva el orden de entrada aunque las ramas terminen en otro orden.
        return list(await asyncio.gather(*(self._rama(s, contexto) for s in solicitudes)))

    async def _rama(
        self, solicitud: SolicitudFuente, contexto: ContextoConsulta
    ) -> ResultadoFuente:
        with operacion(f"perfilamiento.dependencia.fuente.{solicitud.fuente.value}") as op:
            inicio = perf_counter()
            try:
                resultado = await asyncio.wait_for(
                    self._puerto.consultar(solicitud, contexto), timeout=self._timeout_s
                )
            except TimeoutError:
                resultado = _sin_respuesta(solicitud, EstadoFuente.TIEMPO_AGOTADO, inicio)
            except Exception as error:  # noqa: BLE001 - la rama es independiente: nunca se propaga
                # Solo el tipo: el mensaje puede contener datos del cliente o del proveedor.
                logger.warning(
                    "fuente no disponible",
                    extra={"fuente": solicitud.fuente.value, "error_type": type(error).__name__},
                )
                resultado = _sin_respuesta(solicitud, EstadoFuente.NO_DISPONIBLE, inicio)
            op.marcar(_RESULTADO_RAMA.get(resultado.estado, RESULTADO_ERROR))
        return resultado


def _sin_respuesta(
    solicitud: SolicitudFuente, estado: EstadoFuente, inicio: float
) -> ResultadoFuente:
    return ResultadoFuente(
        fuente=solicitud.fuente,
        estado=estado,
        campos_solicitados=solicitud.campos,
        campos={},
        version_fuente=None,
        confianza=None,
        consultada_en=None,
        duracion_ms=round((perf_counter() - inicio) * 1000),
        proveedor=None,
    )
