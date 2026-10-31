"""Barrido de recuperación (HU-M11): reintenta las entregas vencidas y rescata las que quedaron
EN_ENVIO con el lease vencido (caída del proceso). Corre junto al consumidor y no duerme dentro de
una entrega: cada ciclo toma solo lo que ya venció."""

import asyncio
import logging

from avisos.application.entregas.enviar import EnviarEntregas

MENSAJE_CICLO_FALLIDO = "barrido: el ciclo falló; se repite"
MENSAJE_AGOTADAS = "barrido: entregas FALLIDAS por lease vencido sin intentos"

logger = logging.getLogger("avisos.barrido")


class Barrido[T]:
    def __init__(self, enviar: EnviarEntregas[T], intervalo_s: float) -> None:
        self._enviar = enviar
        self._intervalo_s = intervalo_s

    async def ejecutar_una_vez(self) -> int:
        agotadas = await self._enviar.agotar_vencidas()
        if agotadas:
            logger.error(MENSAJE_AGOTADAS, extra={"count": agotadas})
        return await self._enviar.ejecutar()

    async def ejecutar(self, parada: asyncio.Event) -> None:
        while not parada.is_set():
            try:
                await self.ejecutar_una_vez()
            except Exception as error:  # noqa: BLE001 - el siguiente ciclo lo reintenta
                logger.error(MENSAJE_CICLO_FALLIDO, extra={"error_type": type(error).__name__})
            try:
                await asyncio.wait_for(parada.wait(), self._intervalo_s)
            except TimeoutError:
                pass
