"""Convertir las activaciones `PENDIENTE` de un evento en siniestros (HU-W22, DS3-15 y DS3-23;
operación `parametrico.activar`).

Es la **continuación** del consumidor de `parametricos-eventos` (modo (b) de
`solventa_common.inbox`): corre después del commit de la transacción de W21 y antes de borrar el
mensaje. Por cada activación `PENDIENTE` del evento, en paralelo (acotado por `paralelismo`, el
*bulkhead* de 10):

1. arma la solicitud con la cobertura de la póliza y la medición del evento;
2. `POST /v1/siniestros/parametricos` con `Idempotency-Key = <eventoId>:<polizaId>` (timeout de
   600 ms, circuito y *bulkhead* en el adaptador): `201` si lo creó, `200` si ya existía (un timeout
   después del commit de `siniestros` no duplica: el reintento recibe el existente);
3. en una transacción corta, la activación pasa a `SINIESTRO_CREADO` con el siniestro, su código y el
   disparador.

Intenta **todas** las activaciones y, si alguna falla, lanza `ActivacionFallida`: el mensaje no se
borra. Nunca se retiene una conexión de base mientras se espera a `siniestros`. Ni los registros ni
los eventos llevan la medición, la fuente, el cliente ni el cuerpo de la solicitud.
"""

import asyncio
import logging
from uuid import UUID

from solventa_common.resiliencia import TiempoAgotado
from solventa_common.telemetry import operacion

from pagos_parametricos.application.parametrico.puertos import RepositorioActivaciones, Siniestros
from pagos_parametricos.domain.parametrico.activacion import ActivacionPendiente, armar_solicitud
from pagos_parametricos.domain.parametrico.errores import ActivacionFallida
from pagos_parametricos.domain.parametrico.modelos import EventoObservado

OPERACION = "parametrico.activar"
RESULTADO_OK = "ok"
RESULTADO_TIMEOUT = "timeout"
RESULTADO_ERROR = "error"
PARALELISMO = 10
MENSAJE_CIERRE = "parametrico: activacion procesada"
MENSAJE_FALLO = "parametrico: activacion pendiente"

logger = logging.getLogger("pagos_parametricos.activar")


class ActivarSiniestros:
    def __init__(
        self,
        activaciones: RepositorioActivaciones,
        siniestros: Siniestros,
        paralelismo: int = PARALELISMO,
    ) -> None:
        self._activaciones = activaciones
        self._siniestros = siniestros
        self._limite = asyncio.Semaphore(paralelismo)

    async def pendientes(self, evento_id: UUID) -> list[ActivacionPendiente]:
        return await self._activaciones.pendientes(evento_id)

    async def contar_duplicado(self, evento_id: UUID) -> None:
        """La entrega repetida que se reanuda: suma 1 a `duplicados` del evento (DS3-15)."""
        await self._activaciones.sumar_duplicado(evento_id)

    async def ejecutar(self, evento: EventoObservado) -> None:
        """Crea el siniestro de cada activación pendiente del evento; lanza `ActivacionFallida` si
        alguna no pudo (después de intentar todas)."""
        pendientes = await self._activaciones.pendientes(evento.evento_id)
        if not pendientes:
            return
        resultados = await asyncio.gather(
            *(self._activar(evento, p) for p in pendientes), return_exceptions=True
        )
        fallos = [r for r in resultados if isinstance(r, BaseException)]
        # Una cancelación no es un fallo de la activación: se propaga tal cual.
        for fallo in fallos:
            if not isinstance(fallo, Exception):
                raise fallo
        if fallos:
            raise ActivacionFallida(len(fallos), len(pendientes)) from fallos[0]

    async def _activar(self, evento: EventoObservado, pendiente: ActivacionPendiente) -> None:
        async with self._limite:
            with operacion(OPERACION) as op:
                try:
                    solicitud = armar_solicitud(evento, pendiente)
                    creado = await self._siniestros.crear(solicitud)
                    await self._activaciones.marcar_creada(
                        pendiente.evento_id,
                        pendiente.poliza_id,
                        creado,
                        solicitud.disparador,
                    )
                except TiempoAgotado:
                    op.marcar(RESULTADO_TIMEOUT)
                    self._registrar_fallo(pendiente, RESULTADO_TIMEOUT)
                    raise
                except Exception as error:
                    op.marcar(RESULTADO_ERROR)
                    self._registrar_fallo(pendiente, type(error).__name__)
                    raise
                op.marcar(RESULTADO_OK)
                op.anotar(
                    evento_id=str(pendiente.evento_id),
                    poliza_id=str(pendiente.poliza_id),
                    siniestro_id=str(creado.siniestro_id),
                    creado=creado.creado,
                )
                logger.info(
                    MENSAJE_CIERRE,
                    extra={
                        "evento_id": str(pendiente.evento_id),
                        "poliza_id": str(pendiente.poliza_id),
                        "siniestro_id": str(creado.siniestro_id),
                        "operacion": OPERACION,
                        "resultado": RESULTADO_OK,
                    },
                )

    @staticmethod
    def _registrar_fallo(pendiente: ActivacionPendiente, motivo: str) -> None:
        logger.warning(
            MENSAJE_FALLO,
            extra={
                "evento_id": str(pendiente.evento_id),
                "poliza_id": str(pendiente.poliza_id),
                "operacion": OPERACION,
                "resultado": motivo,
            },
        )
