"""Caso de uso: enviar las entregas pendientes (HU-M11).

Lo llaman la continuación del inbox (primer intento, solo las del evento) y el barrido (reintentos y
rescate de leases vencidos). Cada entrega se reclama con una sola sentencia atómica antes de enviar,
así que la continuación, el barrido y varias tareas ECS nunca envían la misma entrega a la vez. No se
duerme en el proceso: un fallo transitorio devuelve la entrega a PENDIENTE con su próximo intento.

Telemetría: la operación `aviso.push` (`ok`, `timeout`, `error`, `rechazado`) sin token ni ids de
dispositivo en las etiquetas. El token rechazado es un resultado `rechazado` (no suma errores) más
un log propio, para que un error del cliente no dispare la alarma del proveedor.
"""

import logging
import time
from datetime import timedelta
from uuid import UUID

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_OK,
    RESULTADO_RECHAZADO,
    registrar,
)

from avisos.application.entregas.puertos import (
    EnviadorPush,
    FalloTransitorio,
    Reloj,
    RepositorioEntregas,
)
from avisos.domain.avisos.modelos import (
    FALLO_INESPERADO,
    FALLO_REINTENTOS_AGOTADOS,
    MOTIVO_DISPOSITIVO_INVALIDO,
    MOTIVO_TOKEN_RECHAZADO,
    Entrega,
    PoliticaReintento,
)

OPERACION = "aviso.push"
MENSAJE_TOKEN_INVALIDO = "aviso: token de dispositivo rechazado; el dispositivo queda INVALIDO"
MENSAJE_FALLIDA = "aviso: entrega FALLIDA; se agotaron los reintentos"
MENSAJE_REINTENTO = "aviso: fallo transitorio; se reintenta"
LOTE_MAXIMO = 50

logger = logging.getLogger("avisos.entregas")


class EnviarEntregas[T]:
    def __init__(
        self,
        entregas: RepositorioEntregas[T],
        enviador: EnviadorPush,
        reloj: Reloj,
        politica: PoliticaReintento,
        lease_s: float,
    ) -> None:
        self._entregas = entregas
        self._enviador = enviador
        self._reloj = reloj
        self._politica = politica
        self._lease = timedelta(seconds=lease_s)

    async def ejecutar(self, event_id: UUID | None = None) -> int:
        """Reclama y envía de una en una, para que el lease cubra solo el envío de cada entrega.
        Devuelve cuántas procesó (`event_id` limita a las de ese evento)."""
        procesadas = 0
        while procesadas < LOTE_MAXIMO:
            ahora = self._reloj.ahora()
            entrega = await self._entregas.reclamar(
                ahora, ahora + self._lease, self._politica.max_intentos, event_id
            )
            if entrega is None:
                break
            await self._enviar(entrega)
            procesadas += 1
        return procesadas

    async def agotar_vencidas(self) -> int:
        return await self._entregas.agotar_vencidas(
            self._reloj.ahora(), self._politica.max_intentos
        )

    async def _enviar(self, entrega: Entrega) -> None:
        if not entrega.dispositivo_activo:
            await self._entregas.marcar_token_invalido(
                entrega, self._reloj.ahora(), MOTIVO_DISPOSITIVO_INVALIDO
            )
            return
        inicio = time.perf_counter()
        codigo = FALLO_INESPERADO
        try:
            aceptado = await self._enviador.enviar(entrega.token, entrega.aviso)
        except FalloTransitorio as fallo:
            codigo = fallo.codigo
        except Exception:  # noqa: BLE001 - el mensaje puede traer el token
            codigo = FALLO_INESPERADO
        else:
            duracion = (time.perf_counter() - inicio) * 1000
            if aceptado:
                registrar(OPERACION, duracion, RESULTADO_OK)
                await self._entregas.marcar_enviada(entrega.id, self._reloj.ahora())
            else:
                registrar(
                    OPERACION, duracion, RESULTADO_RECHAZADO, event_id=str(entrega.aviso.event_id)
                )
                logger.warning(
                    MENSAJE_TOKEN_INVALIDO,
                    extra={
                        "event_id": str(entrega.aviso.event_id),
                        "dispositivo_id": str(entrega.dispositivo_id),
                    },
                )
                await self._entregas.marcar_token_invalido(
                    entrega, self._reloj.ahora(), MOTIVO_TOKEN_RECHAZADO
                )
            return
        registrar(OPERACION, (time.perf_counter() - inicio) * 1000, RESULTADO_ERROR, codigo=codigo)
        ahora = self._reloj.ahora()
        proximo = self._politica.proximo_intento(entrega.intentos, ahora)
        extra = {"event_id": str(entrega.aviso.event_id), "codigo": codigo}
        if proximo is None:
            logger.error(MENSAJE_FALLIDA, extra=extra)
            await self._entregas.marcar_fallida(entrega.id, FALLO_REINTENTOS_AGOTADOS, ahora)
            return
        logger.warning(MENSAJE_REINTENTO, extra=extra | {"intentos": entrega.intentos})
        await self._entregas.reprogramar(entrega.id, proximo, codigo, ahora)
