"""Tarea de plazos del soporte (HU-W20, DS3-13, reescrita en la v1.3).

Los casos `FALTA_SOPORTE` con el plazo vencido vuelven a `EN_EVALUACION` **con el mismo analista**
(la base exige analista en ese estado y la cola «Sin asignar» solo lista `REPORTADO`): el caso
reaparece en su bandeja para evaluarse con la evidencia disponible. El reloj se reanuda y la entrada
`PLAZO_SOPORTE_VENCIDO` del historial y `SiniestroEnEvaluacion` (`motivo = PLAZO_SOPORTE_VENCIDO`)
se escriben en la misma transacción.

Cada lote es una transacción corta con `FOR UPDATE SKIP LOCKED`: dos réplicas nunca toman el mismo
caso. No llama a otros servicios.
"""

import asyncio
import logging
from contextlib import suppress
from typing import Final
from uuid import uuid4

from solventa_common import correlation
from solventa_common.telemetry import RESULTADO_OK, operacion

from siniestros.application.siniestro.puertos import Reloj, SiniestrosRepositorio
from siniestros.domain.siniestro.modelos import EntradaHistorial, EstadoSiniestro
from siniestros.domain.siniestro.transiciones import vencer_plazo_soporte

logger = logging.getLogger("siniestros.plazos")

ACCION_PLAZO_SOPORTE_VENCIDO: Final = "PLAZO_SOPORTE_VENCIDO"
ROL_SISTEMA: Final = "SISTEMA"
MENSAJE_PASADA: Final = "plazos: soportes vencidos"
MENSAJE_PASADA_FALLIDA: Final = "plazos: la pasada falló; se reintenta en la siguiente"


class VencerPlazosSoporte:
    def __init__(self, repo: SiniestrosRepositorio, reloj: Reloj, lote: int) -> None:
        self._repo = repo
        self._reloj = reloj
        self._lote = lote

    async def pasada(self) -> int:
        """Devuelve los casos del plazo vencido a evaluación, en lotes, y dice cuántos fueron."""
        with operacion("siniestro.plazos") as op:
            vencidos = 0
            while True:
                en_lote = await self._lote_vencido()
                vencidos += en_lote
                if en_lote < self._lote:
                    break
            op.marcar(RESULTADO_OK)
            logger.info(MENSAJE_PASADA, extra={"vencidos": vencidos})
            return vencidos

    async def _lote_vencido(self) -> int:
        ahora = self._reloj.ahora()
        async with self._repo.transaccion() as tx:
            casos = await tx.reservar_plazos_vencidos(ahora, self._lote)
            for caso in casos:
                # Los eventos del caso llevan su correlación: se sigue de punta a punta.
                token = correlation.establecer(caso.correlation_id or str(uuid4()))
                try:
                    devuelto = vencer_plazo_soporte(caso, ahora)
                    await tx.guardar_transicion(devuelto, caso.version)
                    await tx.agregar_historial(
                        EntradaHistorial(
                            id=uuid4(),
                            siniestro_id=caso.id,
                            accion=ACCION_PLAZO_SOPORTE_VENCIDO,
                            ocurrido_en=ahora,
                            estado_anterior=EstadoSiniestro.FALTA_SOPORTE,
                            estado_nuevo=EstadoSiniestro.EN_EVALUACION,
                            actor_rol=ROL_SISTEMA,
                            detalles={
                                "plazoVenceEn": (
                                    caso.plazo_soporte_vence_en.isoformat()
                                    if caso.plazo_soporte_vence_en
                                    else None
                                )
                            },
                        )
                    )
                    await tx.evento_en_evaluacion(devuelto, ahora, "PLAZO_SOPORTE_VENCIDO")
                finally:
                    correlation.restablecer(token)
        return len(casos)

    async def ejecutar(self, parada: asyncio.Event, intervalo_s: float) -> None:
        """Una pasada cada `intervalo_s` hasta la parada. Espera antes de la primera: las réplicas
        que arrancan juntas no compiten de golpe. Un fallo se registra y la siguiente pasada
        reintenta."""
        while not await _parada_dentro_de(parada, intervalo_s):
            try:
                await self.pasada()
            except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
                logger.error(MENSAJE_PASADA_FALLIDA, extra={"error_type": type(error).__name__})


async def _parada_dentro_de(parada: asyncio.Event, segundos: float) -> bool:
    """True si llega la parada antes de `segundos`."""
    with suppress(TimeoutError):
        await asyncio.wait_for(parada.wait(), segundos)
    return parada.is_set()
