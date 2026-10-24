"""Caso de uso RegistrarAceptacion (HU-W15, W15-B03).

Se mide con `operacion("aceptacion.registrar")`: `ok` (registrada o repetida), `rechazado` para
un 4xx o `error` para cualquier otra falla. No llama a otros servicios: la decisión guarda la
instantánea de la oferta (DS2-10).

Orden (DS2-11, DS2-15):
1. La Idempotency-Key ya usada devuelve su aceptación (otro cuerpo → IDEMPOTENCIA_CONFLICTO).
2. La decisión de la cotización; si no hay, o si `decisionId` no es el suyo,
   DECISION_NO_ENCONTRADA (sin revelar cuál de los dos casos es).
3. Una cotización ya aceptada con otra clave → OFERTA_YA_ACEPTADA con su `aceptacionId`, antes
   de validar: lo aceptado sigue aceptado aunque la oferta ya haya vencido.
4. `validar_aceptacion`: texto → decisión → vigencia → declaraciones.
5. Guarda la aceptación con su evento en una transacción. La unicidad por cotización y por clave
   resuelve la carrera: si ganó la misma clave, devuelve esa aceptación (200); si ganó otra,
   OFERTA_YA_ACEPTADA.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from polizas.application.aceptacion.puertos import AceptacionesRepositorio
from polizas.application.suscripcion.puertos import DecisionesRepositorio, Reloj
from polizas.domain.aceptacion.errores import (
    DecisionNoAprobada,
    DeclaracionesIncompletas,
    OfertaVencida,
    OfertaYaAceptada,
    TextoNoVigente,
)
from polizas.domain.aceptacion.evidencia import declaraciones_canonicas, huella_evidencia
from polizas.domain.aceptacion.modelos import Aceptacion, CanalAceptacion, Declaraciones
from polizas.domain.aceptacion.validacion import validar_aceptacion
from polizas.domain.suscripcion.errores import ConflictoIdempotencia, DecisionNoEncontrada
from polizas.domain.suscripcion.insumos import huella
from polizas.domain.suscripcion.modelos import Actor, Decision

logger = logging.getLogger("polizas.aceptacion")

OPERACION = "aceptacion.registrar"
ERRORES_DE_NEGOCIO = (
    ConflictoIdempotencia,
    DecisionNoAprobada,
    DecisionNoEncontrada,
    DeclaracionesIncompletas,
    OfertaVencida,
    OfertaYaAceptada,
    TextoNoVigente,
)


@dataclass(frozen=True, slots=True)
class ComandoAceptacion:
    cotizacion_id: UUID
    decision_id: UUID
    version_declaraciones: str
    declaraciones: Declaraciones
    idempotency_key: UUID
    actor: Actor
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ConfigAceptacion:
    # DS2-24: versión vigente del texto de las declaraciones.
    version_declaraciones: str = "2026.10.1"


def huella_solicitud(cmd: ComandoAceptacion) -> str:
    """Lo que identifica el cuerpo (y la ruta): misma clave con otro → IDEMPOTENCIA_CONFLICTO."""
    return huella(
        {
            "cotizacionId": str(cmd.cotizacion_id),
            "decisionId": str(cmd.decision_id),
            "versionDeclaraciones": cmd.version_declaraciones,
            "declaraciones": declaraciones_canonicas(cmd.declaraciones),
        }
    )


class RegistrarAceptacion:
    def __init__(
        self,
        decisiones: DecisionesRepositorio,
        aceptaciones: AceptacionesRepositorio,
        reloj: Reloj,
        config: ConfigAceptacion,
    ) -> None:
        self._decisiones = decisiones
        self._aceptaciones = aceptaciones
        self._reloj = reloj
        self.config = config

    async def ejecutar(self, cmd: ComandoAceptacion) -> tuple[Aceptacion, bool]:
        """La aceptación y si se creó ahora (False: repetición con la misma clave)."""
        with operacion(OPERACION) as op:
            try:
                return await self._ejecutar(cmd)
            except ERRORES_DE_NEGOCIO:
                op.marcar(RESULTADO_RECHAZADO)
                raise

    async def _ejecutar(self, cmd: ComandoAceptacion) -> tuple[Aceptacion, bool]:
        solicitud = huella_solicitud(cmd)
        existente = await self._por_clave(cmd, solicitud)
        if existente is not None:
            return existente, False

        decision = await self._decisiones.por_cotizacion(cmd.cotizacion_id)
        if decision is None or decision.id != cmd.decision_id:
            raise DecisionNoEncontrada()
        previa = await self._aceptaciones.por_cotizacion(cmd.cotizacion_id)
        if previa is not None:
            raise OfertaYaAceptada(previa.id)

        ahora = self._reloj.ahora()
        validar_aceptacion(
            decision,
            cmd.declaraciones,
            cmd.version_declaraciones,
            self.config.version_declaraciones,
            ahora,
        )
        aceptacion = self._nueva(cmd, decision, solicitud, ahora)
        if not await self._aceptaciones.guardar(aceptacion, decision):
            return await self._ganadora(cmd, solicitud), False
        logger.info(
            "aceptacion registrada",
            extra={
                "aceptacion_id": str(aceptacion.id),
                "decision_id": str(decision.id),
                "version_declaraciones": aceptacion.version_declaraciones,
            },
        )
        return aceptacion, True

    def _nueva(
        self, cmd: ComandoAceptacion, decision: Decision, solicitud: str, ahora: datetime
    ) -> Aceptacion:
        sin_huella = Aceptacion(
            id=uuid4(),
            cotizacion_id=cmd.cotizacion_id,
            oferta_id=decision.oferta_id,
            decision_id=decision.id,
            cliente_id=decision.cliente_id,
            huella_calculo=decision.insumos.huella_calculo,
            vigente_hasta=decision.insumos.vigente_hasta,
            version_declaraciones=cmd.version_declaraciones,
            declaraciones=cmd.declaraciones,
            canal=CanalAceptacion.PORTAL_ASESOR,
            actor_id=cmd.actor.id,
            aceptada_en=ahora,
            idempotency_key=cmd.idempotency_key,
            huella_solicitud=solicitud,
            huella_evidencia="",
            correlation_id=cmd.correlation_id,
        )
        return replace(sin_huella, huella_evidencia=huella_evidencia(sin_huella))

    async def _ganadora(self, cmd: ComandoAceptacion, solicitud: str) -> Aceptacion:
        """Perdió la carrera: la misma clave devuelve la ganadora; otra clave, 409."""
        misma_clave = await self._por_clave(cmd, solicitud)
        if misma_clave is not None:
            return misma_clave
        ganadora = await self._aceptaciones.por_cotizacion(cmd.cotizacion_id)
        if ganadora is None:
            # Ni la clave ni la cotización: la clave la ganó otra cotización en ese instante.
            raise ConflictoIdempotencia()
        raise OfertaYaAceptada(ganadora.id)

    async def _por_clave(self, cmd: ComandoAceptacion, solicitud: str) -> Aceptacion | None:
        existente = await self._aceptaciones.por_idempotencia(cmd.idempotency_key)
        if existente is not None and existente.huella_solicitud != solicitud:
            raise ConflictoIdempotencia()
        return existente
