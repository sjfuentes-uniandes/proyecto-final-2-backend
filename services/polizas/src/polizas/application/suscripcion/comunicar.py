"""Caso de uso comunicar_decision (HU-W14, W14-B02): la comunicación de una decisión al leerla.

No se persiste: se deriva de la decisión inmutable, del reloj (una aprobada puede vencer) y de las
alternativas de la versión de reglas con que se decidió. Solo un rechazo pide esas alternativas a
catalogo (`por_version`, caché en memoria de 24 h, DS2-18), con su timeout (DS2-19).

Falla **abierta** solo para las alternativas: si catalogo no responde, la comunicación sale con
`alternativas = []` y se registra en el log, porque la decisión ya está registrada y las
alternativas son solo informativas. La medición es la de la dependencia de catalogo
(`suscripcion.dependencia.catalogo.reglas`), con su resultado `ok`, `timeout` o `error`.
"""

import asyncio
import logging

from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_TIMEOUT, operacion

from polizas.application.suscripcion.puertos import ReglasSuscripcionPuerto, Reloj
from polizas.domain.suscripcion.comunicacion import comunicar
from polizas.domain.suscripcion.errores import DependenciaNoDisponible, ReglasNoVigentes
from polizas.domain.suscripcion.modelos import (
    Alternativa,
    Comunicacion,
    Decision,
    ResultadoDecision,
)

logger = logging.getLogger("polizas.suscripcion")

OPERACION_ALTERNATIVAS = "suscripcion.dependencia.catalogo.reglas"


class ComunicarDecision:
    def __init__(
        self,
        reglas: ReglasSuscripcionPuerto,
        reloj: Reloj,
        compromiso_horas: int,
        catalogo_timeout_ms: int,
    ) -> None:
        self._reglas = reglas
        self._reloj = reloj
        self.compromiso_horas = compromiso_horas
        self._timeout_s = catalogo_timeout_ms / 1000

    async def ejecutar(self, decision: Decision) -> Comunicacion:
        alternativas: tuple[Alternativa, ...] = ()
        if decision.resultado is ResultadoDecision.RECHAZADA:
            alternativas = await self._alternativas(decision)
        return comunicar(decision, alternativas, self._reloj.ahora(), self.compromiso_horas)

    async def _alternativas(self, decision: Decision) -> tuple[Alternativa, ...]:
        with operacion(OPERACION_ALTERNATIVAS) as op:
            try:
                reglas = await asyncio.wait_for(
                    self._reglas.por_version(decision.producto_id, decision.version_reglas),
                    self._timeout_s,
                )
            except (TimeoutError, ReglasNoVigentes, DependenciaNoDisponible) as error:
                resultado = (
                    RESULTADO_TIMEOUT if isinstance(error, TimeoutError) else RESULTADO_ERROR
                )
                op.marcar(resultado)
                logger.warning(
                    "alternativas no disponibles",
                    extra={
                        "decision_id": str(decision.id),
                        "version_reglas": decision.version_reglas,
                        "dependencia": "catalogo",
                        "resultado": resultado,
                    },
                )
                return ()
        return reglas.alternativas
