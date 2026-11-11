"""Evaluar un evento observado, una sola vez (HU-W21, DS3-19 a DS3-21; operación
`parametrico.evaluar`).

En la transacción del inbox, sin llamadas remotas:

1. si el evento ya tiene su decisión (el inbox no lo detuvo, p. ej. por una purga): suma un
   duplicado y conserva la original (AC4);
2. candidatas por índice `(tipo_evento, zona)` sobre las `ACTIVA`. Si el evento nombra una póliza
   (`polizaId`, que la fuente no envía: solo un reproceso dirigido), la candidata es esa póliza, de
   cualquier estado y zona: así salen `POLIZA_NO_ACTIVA` y `FUERA_DE_ZONA`, y si no existe o no
   tiene regla se lanza `PolizaInexistente` o `CoberturaInexistente` (causa `REFERENCIA_INEXISTENTE`
   en H10);
3. evaluación pura;
4. `eventos_evaluados`, `evaluaciones`, `activaciones` `PENDIENTE` y `EventoParametricoEvaluado` en
   el Outbox.

**No crea siniestros** (eso es HU-W22): las activaciones quedan `PENDIENTE`.
"""

import logging
from uuid import UUID, uuid4

from solventa_common.telemetry import operacion

from pagos_parametricos.application.parametrico.puertos import (
    Reloj,
    RepositorioCoberturas,
    RepositorioEvaluaciones,
)
from pagos_parametricos.domain.parametrico.errores import CoberturaInexistente, PolizaInexistente
from pagos_parametricos.domain.parametrico.evaluacion import evaluar
from pagos_parametricos.domain.parametrico.modelos import (
    Cobertura,
    DecisionEvento,
    EvaluacionEvento,
    EventoObservado,
)

OPERACION = "parametrico.evaluar"
DUPLICADO = "duplicado"
RESULTADOS = {
    DecisionEvento.ACTIVA: "activa",
    DecisionEvento.NO_ACTIVA: "no_activa",
    DecisionEvento.SIN_POLIZAS: "sin_polizas",
}
MENSAJE_CIERRE = "parametrico: evento evaluado"

logger = logging.getLogger("pagos_parametricos.evaluar")


class EvaluarEvento[T]:
    def __init__(
        self,
        coberturas: RepositorioCoberturas[T],
        evaluaciones: RepositorioEvaluaciones[T],
        reloj: Reloj,
    ) -> None:
        self._coberturas = coberturas
        self._evaluaciones = evaluaciones
        self._reloj = reloj

    async def ejecutar(
        self,
        evento: EventoObservado,
        correlation_id: str | None,
        tx: T,
        poliza_id: UUID | None = None,
    ) -> EvaluacionEvento | None:
        """La evaluación nueva, o None si el evento ya estaba evaluado."""
        with operacion(OPERACION) as op:
            if await self._evaluaciones.descartar_duplicado(tx, evento.evento_id):
                op.marcar(DUPLICADO)
                logger.info(
                    MENSAJE_CIERRE,
                    extra={"evento_id": str(evento.evento_id), "resultado": DUPLICADO},
                )
                return None
            candidatas = await self._candidatas(evento, tx, poliza_id)
            resultado = evaluar(evento, candidatas)
            await self._evaluaciones.registrar(
                tx, evento, resultado, self._reloj.ahora(), correlation_id or str(uuid4())
            )
            etiqueta = RESULTADOS[resultado.decision]
            op.marcar(etiqueta)
            op.anotar(
                evento_id=str(evento.evento_id),
                fuente=evento.fuente,
                decision=resultado.decision.value,
                evaluadas=resultado.evaluadas,
                activaciones=resultado.activaciones,
            )
            logger.info(
                MENSAJE_CIERRE,
                extra={
                    "evento_id": str(evento.evento_id),
                    "fuente": evento.fuente,
                    "decision": resultado.decision.value,
                    "evaluadas": resultado.evaluadas,
                    "activaciones": resultado.activaciones,
                },
            )
            return resultado

    async def _candidatas(
        self, evento: EventoObservado, tx: T, poliza_id: UUID | None
    ) -> list[Cobertura]:
        if poliza_id is None:
            return await self._coberturas.candidatas(tx, evento.tipo_evento, evento.zona)
        cobertura = await self._coberturas.leer(tx, poliza_id)
        if cobertura is None:
            raise PolizaInexistente
        if cobertura.regla is None:
            raise CoberturaInexistente
        return [cobertura]
