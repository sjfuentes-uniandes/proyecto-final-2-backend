"""Reconstrucción de una decisión (HU-W13, regla 13): con los insumos guardados y la versión de
reglas guardada (pedida a catalogo, que no la cambia nunca), el motor debe dar la misma decisión.

Coincide si la huella de las reglas y la de los insumos son las guardadas y la reevaluación da el
mismo resultado, las mismas reglas evaluadas, la misma regla determinante y los mismos motivos.
"""

import asyncio
from uuid import UUID

from solventa_common.telemetry import RESULTADO_OK, RESULTADO_RECHAZADO, operacion

from polizas.application.suscripcion.puertos import DecisionesRepositorio, ReglasSuscripcionPuerto
from polizas.domain.suscripcion.errores import (
    DecisionNoEncontrada,
    DependenciaNoDisponible,
    ReglasNoVigentes,
)
from polizas.domain.suscripcion.insumos import hash_insumos
from polizas.domain.suscripcion.modelos import Reconstruccion
from polizas.domain.suscripcion.motor import evaluar

RESULTADO_DIFERENTE = "diferente"


class ReconstruirDecision:
    def __init__(
        self, repo: DecisionesRepositorio, reglas: ReglasSuscripcionPuerto, timeout_ms: int
    ) -> None:
        self._repo = repo
        self._reglas = reglas
        self._timeout_s = timeout_ms / 1000

    async def ejecutar(self, decision_id: UUID) -> Reconstruccion:
        with operacion("suscripcion.reconstruir") as op:
            decision = await self._repo.por_id(decision_id)
            if decision is None:
                op.marcar(RESULTADO_RECHAZADO)
                raise DecisionNoEncontrada()
            try:
                reglas = await asyncio.wait_for(
                    self._reglas.por_version(decision.producto_id, decision.version_reglas),
                    self._timeout_s,
                )
            except TimeoutError as error:
                raise DependenciaNoDisponible("catalogo", tiempo_agotado=True) from error
            except (ReglasNoVigentes, DependenciaNoDisponible) as error:
                # La versión guardada debe existir siempre: si no, catalogo violó su contrato.
                raise DependenciaNoDisponible("catalogo") from error
            reevaluada = evaluar(decision.insumos, reglas.reglas)
            coincide = (
                reglas.hash == decision.hash_reglas
                and hash_insumos(decision.insumos) == decision.hash_insumos
                and reevaluada == decision.evaluacion
            )
            op.marcar(RESULTADO_OK if coincide else RESULTADO_DIFERENTE)
        return Reconstruccion(
            coincide=coincide,
            resultado_original=decision.resultado,
            resultado_reconstruido=reevaluada.resultado,
            version_reglas=decision.version_reglas,
            hash_reglas=decision.hash_reglas,
        )
