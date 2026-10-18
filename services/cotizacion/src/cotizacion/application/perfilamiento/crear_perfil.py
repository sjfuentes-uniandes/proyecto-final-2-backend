"""Caso de uso crear_perfil (04 §B.3): calcula, persiste y devuelve un perfil inmutable.

No retiene conexiones de base mientras espera a otros servicios (regla 8): el repositorio
solo se usa en la consulta de idempotencia y al guardar.

Tras la fase B se vuelve a verificar el consentimiento (comprobación final, ADR 0011 §3, HU-W31):
si dejó de estar vigente, lo obtenido se conserva y el perfil queda marcado (ADR 0004); un fallo
técnico de `clientes` falla cerrado y no se guarda nada.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID, uuid4

from solventa_common.telemetry import (
    RESULTADO_ERROR,
    RESULTADO_OK,
    RESULTADO_RECHAZADO,
    RESULTADO_TIMEOUT,
    operacion,
)

from cotizacion.application.perfilamiento.puertos import (
    EjecutorFuentes,
    PuertoConsentimiento,
    PuertoIdentidad,
    PuertoModeloRiesgo,
    Reloj,
    RepositorioPerfiles,
)
from cotizacion.domain.perfilamiento.errores import (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DatosDeclaradosInvalidos,
    DependenciaNoDisponible,
    IdentidadNoVerificada,
    IdentidadPendiente,
    ModeloNoDisponible,
)
from cotizacion.domain.perfilamiento.modelo_riesgo import ModeloRiesgo
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    ContextoConsulta,
    DatosDeclarados,
    EstadoCampo,
    EstadoFuente,
    EstadoIdentidad,
    EstadoPerfil,
    Fuente,
    Perfil,
    PlanFuentes,
    ResultadoFuente,
    VerificacionConsentimiento,
)
from cotizacion.domain.perfilamiento.motor import FuenteEvaluada, evaluar

logger = logging.getLogger("cotizacion.perfilamiento")

RESULTADO_DEGRADADO = "degradado"
RESULTADO_INSUFICIENTE = "insuficiente"
_RESULTADO_POR_ESTADO = {
    EstadoPerfil.CALCULADO: RESULTADO_OK,
    EstadoPerfil.DEGRADADO: RESULTADO_DEGRADADO,
    EstadoPerfil.INFORMACION_INSUFICIENTE: RESULTADO_INSUFICIENTE,
}
# Errores de negocio (4xx): la operación queda "rechazado", no "error".
ERRORES_DE_NEGOCIO: tuple[type[Exception], ...] = (
    ClienteNoEncontrado,
    ConflictoIdempotencia,
    DatosDeclaradosInvalidos,
    IdentidadNoVerificada,
    IdentidadPendiente,
    ModeloNoDisponible,
)
_RESULTADO_FUENTE = {
    EstadoFuente.CONSULTADA: RESULTADO_OK,
    EstadoFuente.TIEMPO_AGOTADO: RESULTADO_TIMEOUT,
}
EDAD_MAXIMA = 120
CAMPO_FECHA_NACIMIENTO = "datosDeclarados.fechaNacimiento"
DEPENDENCIA_CONSENTIMIENTO = "clientes.consentimiento"
COMPROBACION_FINAL = "clientes.comprobacion_final"
# Paso propio del perfilamiento, no una dependencia más de las fases A y B.
OPERACION_COMPROBACION_FINAL = "perfilamiento.comprobacion_final"

T = TypeVar("T")
PlanificadorFuentes = Callable[[ModeloRiesgo, VerificacionConsentimiento], PlanFuentes]


@dataclass(frozen=True, slots=True)
class ComandoPerfil:
    cliente_id: UUID
    producto_id: str
    datos_declarados: DatosDeclarados
    idempotency_key: UUID
    actor_id: str
    correlation_id: str
    perfil_anterior_id: UUID | None = None
    operacion: str = "perfilamiento"


@dataclass(frozen=True, slots=True)
class ConfigPerfilamiento:
    """Presupuestos de DT-10, en milisegundos."""

    dependencia_timeout_ms: int = 150
    fuente_timeout_ms: int = 300
    presupuesto_ms: int = 400


@dataclass(slots=True)
class _Dependencias:
    """Duración y resultado de cada dependencia, para el log de cierre (DT-11)."""

    registros: list[dict[str, Any]] = field(default_factory=list)

    def agregar(self, nombre: str, ms: float, resultado: str) -> None:
        self.registros.append({"nombre": nombre, "ms": round(ms, 2), "resultado": resultado})


def huella_solicitud(cmd: ComandoPerfil) -> str:
    """sha256 del JSON canónico de lo que identifica la solicitud (DT-26)."""
    declarados = cmd.datos_declarados
    contenido = {
        "clienteId": str(cmd.cliente_id),
        "productoId": cmd.producto_id,
        "datosDeclarados": {
            "fechaNacimiento": declarados.fecha_nacimiento.isoformat(),
            "plazoCreditoMeses": declarados.plazo_credito_meses,
            "sumaAsegurada": str(declarados.suma_asegurada),
        },
        "perfilAnteriorId": str(cmd.perfil_anterior_id) if cmd.perfil_anterior_id else None,
    }
    canonico = json.dumps(contenido, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def edad_cumplida(nacimiento: date, hoy: date) -> int:
    """Años cumplidos a la fecha del cálculo (DT-27)."""
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


class CrearPerfil:
    def __init__(
        self,
        identidad: PuertoIdentidad,
        consentimiento: PuertoConsentimiento,
        modelos: PuertoModeloRiesgo,
        plan_fuentes: PlanificadorFuentes,
        ejecutor: EjecutorFuentes,
        repo: RepositorioPerfiles,
        reloj: Reloj,
        config: ConfigPerfilamiento,
        comprobacion: PuertoConsentimiento | None = None,
    ) -> None:
        """`comprobacion` atiende la comprobación final; por omisión, el puerto de la fase A."""
        self._identidad = identidad
        self._consentimiento = consentimiento
        self._comprobacion = comprobacion or consentimiento
        self._modelos = modelos
        self._plan_fuentes = plan_fuentes
        self._ejecutor = ejecutor
        self._repo = repo
        self._reloj = reloj
        self.config = config

    async def ejecutar(self, cmd: ComandoPerfil) -> tuple[Perfil, bool]:
        with operacion(cmd.operacion) as op:
            try:
                perfil, creado = await self._ejecutar(cmd)
            except ERRORES_DE_NEGOCIO:
                op.marcar(RESULTADO_RECHAZADO)
                raise
            op.marcar(_RESULTADO_POR_ESTADO[perfil.resultado.estado])
        return perfil, creado

    async def _ejecutar(self, cmd: ComandoPerfil) -> tuple[Perfil, bool]:
        huella = huella_solicitud(cmd)
        existente = await self._existente(cmd, huella)
        if existente is not None:
            return existente, False

        ahora = self._reloj.ahora()
        inicio = time.perf_counter()
        edad = edad_cumplida(cmd.datos_declarados.fecha_nacimiento, ahora.date())
        if cmd.datos_declarados.fecha_nacimiento > ahora.date() or edad > EDAD_MAXIMA:
            raise DatosDeclaradosInvalidos((CAMPO_FECHA_NACIMIENTO,))

        dependencias = _Dependencias()
        verificacion, modelo = await self._fase_a(cmd, ahora, dependencias)
        plan = self._plan_fuentes(modelo, verificacion)
        resultados = await self._fase_b(cmd, plan, verificacion, dependencias)
        comprobaciones = _Dependencias()
        comprobacion = await self._comprobacion_final(cmd, plan, comprobaciones)
        fuentes = _todas_las_fuentes(modelo, plan, resultados)

        variables = {
            "edad": Decimal(edad),
            "plazo_credito_meses": Decimal(cmd.datos_declarados.plazo_credito_meses),
            "suma_asegurada": cmd.datos_declarados.suma_asegurada,
        }
        for resultado in fuentes:
            if resultado.estado is EstadoFuente.CONSULTADA:
                for campo, valor in resultado.campos.items():
                    if valor.estado is EstadoCampo.DISPONIBLE and valor.valor is not None:
                        variables[campo] = valor.valor
        evaluacion = evaluar(
            modelo, variables, {r.fuente: FuenteEvaluada(r.estado, r.confianza) for r in fuentes}
        )
        perfil = Perfil(
            id=uuid4(),
            cliente_id=cmd.cliente_id,
            producto_id=cmd.producto_id,
            idempotency_key=cmd.idempotency_key,
            huella_solicitud=huella,
            datos_declarados=cmd.datos_declarados,
            insumos=variables,
            resultado=evaluacion,
            fuentes=fuentes,
            consentimiento=verificacion,
            version_modelo=modelo.version,
            hash_modelo=modelo.hash,
            perfil_anterior_id=cmd.perfil_anterior_id,
            calculado_en=self._reloj.ahora(),
            duracion_ms=round((time.perf_counter() - inicio) * 1000),
            correlation_id=cmd.correlation_id,
            actor_id=cmd.actor_id,
            comprobacion_final=comprobacion,
        )
        try:
            await self._repo.guardar(perfil)
        except ConflictoIdempotencia:
            # Carrera con otra solicitud de la misma clave: gana la que guardó primero.
            ganador = await self._existente(cmd, huella)
            if ganador is None:
                raise
            return ganador, False
        logger.info(
            "perfil calculado",
            extra={
                "perfil_id": str(perfil.id),
                "estado": perfil.resultado.estado.value,
                "dependencias": dependencias.registros,
                "comprobacion_final": next(iter(comprobaciones.registros), None),
                "revocado_durante_perfilamiento": perfil.revocado_durante_perfilamiento,
            },
        )
        return perfil, True

    async def _existente(self, cmd: ComandoPerfil, huella: str) -> Perfil | None:
        existente = await self._repo.por_idempotencia(cmd.cliente_id, cmd.idempotency_key)
        if existente is not None and existente.huella_solicitud != huella:
            raise ConflictoIdempotencia()
        return existente

    async def _fase_a(
        self, cmd: ComandoPerfil, ahora: datetime, dependencias: _Dependencias
    ) -> tuple[VerificacionConsentimiento, ModeloRiesgo]:
        """Identidad, consentimiento y modelo en paralelo; falla cerrada ante errores técnicos."""
        llamadas: Sequence[tuple[str, Callable[[], Awaitable[Any]]]] = (
            ("clientes.identidad", lambda: self._identidad.estado(cmd.cliente_id)),
            (
                DEPENDENCIA_CONSENTIMIENTO,
                lambda: self._consentimiento.verificar(
                    cmd.cliente_id, PROPOSITO_PERFILAMIENTO, list(Fuente)
                ),
            ),
            ("catalogo.modelo", lambda: self._modelos.vigente(cmd.producto_id, ahora)),
        )
        resultados = await asyncio.gather(
            *(self._medir(nombre, llamada, dependencias) for nombre, llamada in llamadas),
            return_exceptions=True,
        )
        nombres = [nombre for nombre, _ in llamadas]
        # 1. Cliente inexistente; 2. falla técnica; 3. identidad; 4. modelo (04 §B.3).
        for resultado in resultados:
            if isinstance(resultado, ClienteNoEncontrado):
                raise resultado
        for nombre, resultado in zip(nombres, resultados, strict=True):
            if isinstance(resultado, TimeoutError | DependenciaNoDisponible):
                raise DependenciaNoDisponible(nombre) from resultado
        identidad, verificacion, modelo = resultados
        if isinstance(identidad, BaseException):
            raise identidad
        if identidad is EstadoIdentidad.RECHAZADO:
            raise IdentidadNoVerificada()
        if identidad is EstadoIdentidad.PENDIENTE:
            raise IdentidadPendiente()
        if isinstance(verificacion, BaseException):
            raise verificacion
        if isinstance(modelo, BaseException):
            raise modelo
        return verificacion, modelo

    async def _medir(
        self,
        nombre: str,
        llamada: Callable[[], Awaitable[T]],
        dependencias: _Dependencias,
        nombre_operacion: str | None = None,
    ) -> T:
        inicio = time.perf_counter()
        resultado = RESULTADO_OK
        try:
            with operacion(nombre_operacion or f"perfilamiento.dependencia.{nombre}") as op:
                try:
                    valor = await asyncio.wait_for(
                        llamada(), self.config.dependencia_timeout_ms / 1000
                    )
                except (ClienteNoEncontrado, ModeloNoDisponible):
                    # La dependencia respondió bien: el "no existe" es de negocio.
                    op.marcar(RESULTADO_OK)
                    raise
            return valor
        except TimeoutError:
            resultado = RESULTADO_TIMEOUT
            raise
        except DependenciaNoDisponible:
            resultado = RESULTADO_ERROR
            raise
        finally:
            dependencias.agregar(nombre, (time.perf_counter() - inicio) * 1000, resultado)

    async def _fase_b(
        self,
        cmd: ComandoPerfil,
        plan: PlanFuentes,
        verificacion: VerificacionConsentimiento,
        dependencias: _Dependencias,
    ) -> list[ResultadoFuente]:
        if not plan.a_consultar:
            return []
        contexto = ContextoConsulta(
            cliente_id=cmd.cliente_id,
            consentimiento_id=verificacion.consentimiento_id,
            proposito=PROPOSITO_PERFILAMIENTO,
            deadline_ms=self.config.fuente_timeout_ms - 10,
        )
        resultados = await self._ejecutor.ejecutar(plan.a_consultar, contexto)
        for r in resultados:
            resultado = _RESULTADO_FUENTE.get(r.estado, RESULTADO_ERROR)
            dependencias.agregar(f"fuente.{r.fuente.value}", r.duracion_ms or 0, resultado)
        return resultados

    async def _comprobacion_final(
        self, cmd: ComandoPerfil, plan: PlanFuentes, comprobaciones: _Dependencias
    ) -> VerificacionConsentimiento | None:
        """Vuelve a verificar los alcances consultados, solo para dejar constancia (ADR 0011 §3).

        Sin fuentes consultadas no hay nada que comprobar. No cancela ni repite consultas. Un
        timeout o una falla técnica de `clientes` falla cerrado: el perfil no se entrega.
        """
        if not plan.a_consultar:
            return None
        alcances = [s.fuente for s in plan.a_consultar]
        try:
            return await self._medir(
                COMPROBACION_FINAL,
                lambda: self._comprobacion.verificar(
                    cmd.cliente_id, PROPOSITO_PERFILAMIENTO, alcances
                ),
                comprobaciones,
                OPERACION_COMPROBACION_FINAL,
            )
        except (TimeoutError, DependenciaNoDisponible) as error:
            raise DependenciaNoDisponible(DEPENDENCIA_CONSENTIMIENTO) from error


def _todas_las_fuentes(
    modelo: ModeloRiesgo, plan: PlanFuentes, resultados: Sequence[ResultadoFuente]
) -> tuple[ResultadoFuente, ...]:
    """Una entrada por fuente del modelo, en su orden; las no consultadas, sintéticas."""
    por_fuente = {r.fuente: r for r in resultados}
    return tuple(
        por_fuente.get(fuente)
        or ResultadoFuente(
            fuente=fuente,
            estado=EstadoFuente.NO_AUTORIZADA
            if fuente in plan.no_autorizadas
            else EstadoFuente.NO_CONSULTADA,
            campos_solicitados=(),
            campos={},
            version_fuente=None,
            confianza=None,
            consultada_en=None,
            duracion_ms=None,
            proveedor=None,
        )
        for fuente in modelo.fuentes
    )
