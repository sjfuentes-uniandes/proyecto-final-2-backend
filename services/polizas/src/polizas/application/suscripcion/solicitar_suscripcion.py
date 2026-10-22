"""Caso de uso solicitar_suscripcion (HU-W13, W13-B15).

Se mide con `operacion("suscripcion.evaluar")`, cuyo resultado es el de la decisión (`aprobada`,
`rechazada`, `revision`), `rechazado` para un 4xx o `error`/`timeout` para un 5xx.

Orden (DS2-05, DS2-17, DS2-10):
1. La Idempotency-Key ya usada devuelve su decisión (otro cuerpo → IDEMPOTENCIA_CONFLICTO).
2. Una cotización ya decidida devuelve su decisión: nunca hay dos decisiones por cotización.
3. Insumos de `cotizacion` y luego reglas vigentes de `catalogo`, cada uno con su timeout
   (DS2-19) y sin conexión de base abierta. Van en serie porque las reglas dependen del producto
   de la cotización; las de catalogo suelen salir de la caché en memoria (DS2-18).
4. Identidad no VERIFICADO u oferta vencida: sin decisión.
5. Evalúa y guarda la decisión con su evento en una transacción. Si otra solicitud ganó la
   carrera, devuelve la decisión ganadora.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_OK,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from polizas.application.suscripcion.puertos import (
    DecisionesRepositorio,
    GeneradorCodigos,
    InsumosOfertaPuerto,
    ReglasSuscripcionPuerto,
    Reloj,
)
from polizas.domain.suscripcion.errores import (
    ConflictoIdempotencia,
    CotizacionNoEncontrada,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    OfertaVencida,
    ReglasNoVigentes,
)
from polizas.domain.suscripcion.insumos import hash_insumos, huella
from polizas.domain.suscripcion.modelos import (
    Actor,
    Decision,
    EstadoIdentidad,
    InsumosSuscripcion,
    ResultadoDecision,
    TipoDecision,
)
from polizas.domain.suscripcion.motor import evaluar
from polizas.domain.suscripcion.vigencia import oferta_vigente

logger = logging.getLogger("polizas.suscripcion")

OPERACION = "suscripcion.evaluar"
DEPENDENCIA_INSUMOS = "cotizacion.insumos"
DEPENDENCIA_REGLAS = "catalogo.reglas"
RESULTADO_DE: dict[ResultadoDecision, str] = {
    ResultadoDecision.APROBADA: "aprobada",
    ResultadoDecision.RECHAZADA: "rechazada",
    ResultadoDecision.REVISION_REQUERIDA: "revision",
}
ERRORES_DE_NEGOCIO = (
    ConflictoIdempotencia,
    CotizacionNoEncontrada,
    IdentidadNoVerificada,
    IdentidadPendiente,
    OfertaVencida,
    ReglasNoVigentes,
)
# Respuestas funcionales de una dependencia que respondió bien.
_NO_EXISTE = (CotizacionNoEncontrada, ReglasNoVigentes)


@dataclass(frozen=True, slots=True)
class ComandoSuscripcion:
    cotizacion_id: UUID
    idempotency_key: UUID
    actor: Actor
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ConfigSuscripcion:
    cotizacion_timeout_ms: int = 600
    catalogo_timeout_ms: int = 300


@dataclass
class _Dependencias:
    """Duración y resultado de cada dependencia, para el log de cierre (lista: `sanitizar` no
    redacta valores por el nombre de la clave)."""

    registros: list[dict[str, Any]] = field(default_factory=list)

    def agregar(self, nombre: str, ms: float, resultado: str) -> None:
        self.registros.append({"nombre": nombre, "ms": round(ms, 1), "resultado": resultado})


def huella_solicitud(cmd: ComandoSuscripcion) -> str:
    return huella({"cotizacionId": str(cmd.cotizacion_id)})


def verificar_identidad(insumos: InsumosSuscripcion) -> None:
    """DS2-17: la suscripción exige identidad VERIFICADO; es una precondición, no una regla."""
    if insumos.estado_identidad is EstadoIdentidad.RECHAZADO:
        raise IdentidadNoVerificada()
    if insumos.estado_identidad is EstadoIdentidad.PENDIENTE:
        raise IdentidadPendiente()


class SolicitarSuscripcion:
    def __init__(
        self,
        insumos: InsumosOfertaPuerto,
        reglas: ReglasSuscripcionPuerto,
        repo: DecisionesRepositorio,
        codigos: GeneradorCodigos,
        reloj: Reloj,
        config: ConfigSuscripcion,
    ) -> None:
        self._insumos = insumos
        self._reglas = reglas
        self._repo = repo
        self._codigos = codigos
        self._reloj = reloj
        self.config = config

    async def ejecutar(self, cmd: ComandoSuscripcion) -> tuple[Decision, bool]:
        """La decisión y si se creó ahora (False: repetición o decisión existente)."""
        with operacion(OPERACION) as op:
            try:
                decision, creada = await self._ejecutar(cmd)
            except ERRORES_DE_NEGOCIO:
                op.marcar(RESULTADO_RECHAZADO)
                raise
            except DependenciaNoDisponible as error:
                op.marcar(RESULTADO_TIMEOUT if error.tiempo_agotado else RESULTADO_ERROR)
                raise
            op.marcar(RESULTADO_DE[decision.resultado])
        return decision, creada

    async def _ejecutar(self, cmd: ComandoSuscripcion) -> tuple[Decision, bool]:
        solicitud = huella_solicitud(cmd)
        existente = await self._por_clave(cmd, solicitud)
        if existente is None:
            existente = await self._repo.por_cotizacion(cmd.cotizacion_id)
        if existente is not None:
            return existente, False

        inicio = time.perf_counter()
        dependencias = _Dependencias()
        insumos = await self._medir(
            DEPENDENCIA_INSUMOS,
            lambda: self._insumos.insumos(cmd.cotizacion_id, cmd.actor),
            self.config.cotizacion_timeout_ms,
            dependencias,
        )
        verificar_identidad(insumos)
        ahora = self._reloj.ahora()
        if not oferta_vigente(insumos.vigente_hasta, ahora):
            raise OfertaVencida()
        reglas = await self._medir(
            DEPENDENCIA_REGLAS,
            lambda: self._reglas.vigentes(insumos.producto_id),
            self.config.catalogo_timeout_ms,
            dependencias,
        )
        evaluacion = evaluar(insumos, reglas.reglas)
        decision = Decision(
            id=uuid4(),
            codigo=await self._codigos.siguiente(ahora.year),
            cotizacion_id=cmd.cotizacion_id,
            oferta_id=insumos.oferta_id,
            cotizacion_codigo=insumos.codigo,
            cliente_id=insumos.cliente_id,
            producto_id=insumos.producto_id,
            tipo=TipoDecision.AUTOMATICA,
            evaluacion=evaluacion,
            version_reglas=reglas.version,
            hash_reglas=reglas.hash,
            insumos=insumos,
            hash_insumos=hash_insumos(insumos),
            decidida_en=ahora,
            duracion_ms=round((time.perf_counter() - inicio) * 1000),
            idempotency_key=cmd.idempotency_key,
            huella_solicitud=solicitud,
            correlation_id=cmd.correlation_id,
            actor_id=cmd.actor.id,
        )
        if not await self._repo.guardar(decision):
            # Carrera con otra solicitud de la misma cotización o clave: gana la primera.
            ganadora = await self._repo.por_cotizacion(cmd.cotizacion_id)
            if ganadora is None:
                ganadora = await self._por_clave(cmd, solicitud)
            if ganadora is None:
                raise ConflictoIdempotencia()
            return ganadora, False
        logger.info(
            "suscripcion decidida",
            extra={
                "decision_id": str(decision.id),
                "resultado": decision.resultado.value,
                "version_reglas": decision.version_reglas,
                "dependencias": dependencias.registros,
            },
        )
        return decision, True

    async def _por_clave(self, cmd: ComandoSuscripcion, solicitud: str) -> Decision | None:
        existente = await self._repo.por_idempotencia(cmd.idempotency_key)
        if existente is not None and existente.huella_solicitud != solicitud:
            raise ConflictoIdempotencia()
        return existente

    async def _medir[T](
        self,
        nombre: str,
        llamada: Callable[[], Awaitable[T]],
        timeout_ms: int,
        dependencias: _Dependencias,
    ) -> T:
        """Una dependencia con su timeout; un timeout o una falla técnica es falla cerrada."""
        inicio = time.perf_counter()
        resultado = RESULTADO_OK
        servicio = nombre.split(".")[0]
        try:
            with operacion(f"suscripcion.dependencia.{nombre}") as op:
                try:
                    return await asyncio.wait_for(llamada(), timeout_ms / 1000)
                except _NO_EXISTE:
                    op.marcar(RESULTADO_OK)
                    raise
        except TimeoutError as error:
            resultado = RESULTADO_TIMEOUT
            raise DependenciaNoDisponible(servicio, tiempo_agotado=True) from error
        except DependenciaNoDisponible as error:
            resultado = RESULTADO_ERROR
            raise DependenciaNoDisponible(servicio) from error
        finally:
            dependencias.agregar(nombre, (time.perf_counter() - inicio) * 1000, resultado)
