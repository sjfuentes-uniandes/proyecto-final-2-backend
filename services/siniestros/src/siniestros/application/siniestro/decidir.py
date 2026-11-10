"""Decisión del analista sobre un caso EN_EVALUACION (HU-W20, DS3-12): aprobar, rechazar o solicitar
soporte.

En **una sola transacción** (sin llamadas a otros servicios ni a S3):

1. la `Idempotency-Key` (clave `decision:<Idempotency-Key>` de la tabla `idempotencia`): si ya está,
   es una repetición (mismo cuerpo: devuelve el resultado original) o un conflicto;
2. el bloqueo del siniestro (`FOR UPDATE`): dos decisiones sobre el mismo caso se serializan;
3. la transición y el analista asignado (la guía de W20 fija ese orden);
4. el contenido que necesita el caso (evidencias y liquidación);
5. la decisión inmutable, el cambio de estado (pausa o reanudación del reloj), la entrada del
   historial y la fila del Outbox.

Las comprobaciones que no necesitan la base (justificación, causal, textos al cliente, documentos y
plazo) van antes de abrir la transacción. Ni la explicación ni el mensaje al cliente salen de la fila
de la decisión: no van al historial, a los eventos ni a los registros.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Final
from uuid import UUID, uuid4

from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import operacion

from siniestros.application.siniestro.errores import (
    CasoAsignadoAOtro,
    CausalInvalida,
    IdempotenciaConflicto,
    JustificacionRequerida,
    SiniestroNoEncontrado,
    SolicitudInvalida,
    TransicionNoPermitida,
    ValorFueraDeRango,
)
from siniestros.application.siniestro.puertos import (
    DecisionFinalDuplicada,
    Reloj,
    SiniestrosRepositorio,
    TxSiniestros,
)
from siniestros.application.siniestro.vistas import Actor, DecisionRegistrada
from siniestros.domain.siniestro import errores as dominio
from siniestros.domain.siniestro.modelos import (
    AccionSiniestro,
    CanalSoporte,
    CausalRechazo,
    EntradaHistorial,
    EstadoSiniestro,
    Siniestro,
    SoporteSolicitable,
)
from siniestros.domain.siniestro.transiciones import (
    Liquidacion,
    aprobar,
    normalizar_monto,
    rechazar,
    referencia_pago,
    solicitar_soporte,
    validar_causal,
    validar_documentos,
    validar_justificacion,
    validar_liquidacion,
    validar_plazo,
    validar_texto_cliente,
    verificar_decision,
)

_OPERACION = "POST /v1/siniestros/{id}/decision"
PREFIJO_CLAVE: Final = "decision:"

# Acciones del historial (códigos en mayúsculas, R-10).
ACCION_APROBADO: Final = "APROBADO"
ACCION_RECHAZADO: Final = "RECHAZADO"
ACCION_SOPORTE_SOLICITADO: Final = "SOPORTE_SOLICITADO"

# Resultados de `siniestro.decidir` (guía W20, R-69). `rechazado` es la decisión de rechazar.
RESULTADO_APROBADO: Final = "aprobado"
RESULTADO_RECHAZADO: Final = "rechazado"
RESULTADO_SOPORTE: Final = "soporte"
RESULTADO_REPETIDO: Final = "repetido"
RESULTADO_TRANSICION_INVALIDA: Final = "transicion_invalida"
RESULTADO_INVALIDA: Final = "invalida"


# --- Lo que pide cada decisión (ya con tipos, sin validar) ---------------------------------------


@dataclass(frozen=True)
class SolicitudAprobar:
    justificacion: str
    evidencias_consideradas: tuple[UUID, ...]
    valor_reclamado: str
    deducible: str
    valor_indemnizar: str
    moneda: str


@dataclass(frozen=True)
class SolicitudRechazar:
    justificacion: str
    evidencias_consideradas: tuple[UUID, ...]
    causal: str
    explicacion_cliente: str


@dataclass(frozen=True)
class SolicitudSoportar:
    justificacion: str
    evidencias_consideradas: tuple[UUID, ...]
    documentos: tuple[SoporteSolicitable, ...]
    mensaje_cliente: str
    canal: CanalSoporte
    plazo_dias_habiles: int


Solicitud = SolicitudAprobar | SolicitudRechazar | SolicitudSoportar


@dataclass(frozen=True)
class ResultadoDecision:
    siniestro_id: UUID
    estado_nuevo: EstadoSiniestro
    decision_id: UUID
    referencia_pago: str | None
    plazo_soporte_vence_en: datetime | None
    registrada_en: datetime

    def a_json(self) -> dict[str, Any]:
        """Lo que se guarda con la clave: se devuelve tal cual en la repetición."""
        return {
            "siniestroId": str(self.siniestro_id),
            "estadoNuevo": self.estado_nuevo.value,
            "decisionId": str(self.decision_id),
            "referenciaPago": self.referencia_pago,
            "plazoSoporteVenceEn": (
                self.plazo_soporte_vence_en.isoformat() if self.plazo_soporte_vence_en else None
            ),
            "registradaEn": self.registrada_en.isoformat(),
        }

    @classmethod
    def de_json(cls, datos: Mapping[str, Any]) -> "ResultadoDecision":
        vence = datos["plazoSoporteVenceEn"]
        return cls(
            siniestro_id=UUID(datos["siniestroId"]),
            estado_nuevo=EstadoSiniestro(datos["estadoNuevo"]),
            decision_id=UUID(datos["decisionId"]),
            referencia_pago=datos["referenciaPago"],
            plazo_soporte_vence_en=datetime.fromisoformat(vence) if vence else None,
            registrada_en=datetime.fromisoformat(datos["registradaEn"]),
        )


# --- Comprobaciones sin base de datos (pasos 1 y 2 del orden) ------------------------------------


@dataclass(frozen=True)
class _Normalizada:
    accion: AccionSiniestro
    justificacion: str
    evidencias: tuple[UUID, ...]
    cuerpo: dict[str, Any]  # el cuerpo normalizado: de aquí sale la huella


def _traducir(error: Exception) -> ErrorNegocio:
    """Los errores del dominio, como los errores estables de la API."""
    if isinstance(error, dominio.JustificacionRequerida):
        return JustificacionRequerida()
    if isinstance(error, dominio.CausalInvalida):
        return CausalInvalida()
    if isinstance(error, dominio.ValorFueraDeRango):
        return ValorFueraDeRango(error.campos)
    if isinstance(error, dominio.DecisionInvalida):
        return SolicitudInvalida(error.campos)
    raise error


def _normalizar(solicitud: Solicitud) -> _Normalizada:
    justificacion = validar_justificacion(solicitud.justificacion)
    evidencias = tuple(sorted(set(solicitud.evidencias_consideradas), key=str))
    comun: dict[str, Any] = {
        "justificacion": justificacion,
        "evidencias": [str(e) for e in evidencias],
    }
    if isinstance(solicitud, SolicitudAprobar):
        return _Normalizada(
            AccionSiniestro.APROBAR,
            justificacion,
            evidencias,
            {
                **comun,
                "tipo": "APROBAR",
                "valorReclamado": normalizar_monto(solicitud.valor_reclamado),
                "deducible": normalizar_monto(solicitud.deducible),
                "valorIndemnizar": normalizar_monto(solicitud.valor_indemnizar),
                "moneda": solicitud.moneda.strip(),
            },
        )
    if isinstance(solicitud, SolicitudRechazar):
        causal = validar_causal(solicitud.causal)
        explicacion = validar_texto_cliente("explicacionCliente", solicitud.explicacion_cliente)
        return _Normalizada(
            AccionSiniestro.RECHAZAR,
            justificacion,
            evidencias,
            {
                **comun,
                "tipo": "RECHAZAR",
                "causal": causal.value,
                "explicacionCliente": explicacion,
            },
        )
    documentos = validar_documentos(solicitud.documentos)
    mensaje = validar_texto_cliente("mensajeCliente", solicitud.mensaje_cliente)
    plazo = validar_plazo(solicitud.plazo_dias_habiles)
    return _Normalizada(
        AccionSiniestro.SOLICITAR_SOPORTE,
        justificacion,
        evidencias,
        {
            **comun,
            "tipo": "SOLICITAR_SOPORTE",
            "documentos": sorted(d.value for d in documentos),
            "mensajeCliente": mensaje,
            "canal": solicitud.canal.value,
            "plazoDiasHabiles": plazo,
        },
    )


def huella_de(siniestro_id: UUID, actor_id: str, cuerpo: Mapping[str, Any]) -> str:
    """Identifica la solicitud: la misma clave con otro cuerpo, actor o siniestro es un conflicto."""
    canonico = json.dumps(
        {"op": _OPERACION, "siniestroId": str(siniestro_id), "actor": actor_id, "cuerpo": cuerpo},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonico.encode()).hexdigest()


# --- El caso de uso ------------------------------------------------------------------------------


class Decidir:
    def __init__(self, repo: SiniestrosRepositorio, reloj: Reloj) -> None:
        self._repo = repo
        self._reloj = reloj

    async def ejecutar(
        self,
        siniestro_id: UUID,
        actor: Actor,
        analista_id: UUID,
        clave: UUID,
        solicitud: Solicitud,
    ) -> ResultadoDecision:
        with operacion("siniestro.decidir") as op:
            try:
                resultado, marca = await self._decidir(
                    siniestro_id, actor, analista_id, clave, solicitud
                )
            except TransicionNoPermitida:
                op.marcar(RESULTADO_TRANSICION_INVALIDA)
                raise
            except ErrorNegocio:
                op.marcar(RESULTADO_INVALIDA)
                raise
            op.marcar(marca)
            return resultado

    async def _decidir(
        self,
        siniestro_id: UUID,
        actor: Actor,
        analista_id: UUID,
        clave: UUID,
        solicitud: Solicitud,
    ) -> tuple[ResultadoDecision, str]:
        try:
            normalizada = _normalizar(solicitud)
        except (
            dominio.JustificacionRequerida,
            dominio.CausalInvalida,
            dominio.DecisionInvalida,
        ) as e:
            raise _traducir(e) from e
        huella = huella_de(siniestro_id, actor.id, normalizada.cuerpo)
        ahora = self._reloj.ahora()
        clave_guardada = f"{PREFIJO_CLAVE}{clave}"
        try:
            async with self._repo.transaccion() as tx:
                previo = await tx.adquirir_idempotencia(clave_guardada)
                if previo is not None:
                    if previo.huella != huella:
                        raise IdempotenciaConflicto
                    return ResultadoDecision.de_json(previo.respuesta), RESULTADO_REPETIDO
                caso = await tx.bloquear_siniestro(siniestro_id)
                if caso is None:
                    raise SiniestroNoEncontrado
                try:
                    destino = verificar_decision(caso, normalizada.accion, analista_id)
                except dominio.TransicionInvalida as error:
                    raise TransicionNoPermitida(str(error.desde)) from error
                except dominio.CasoAsignadoAOtro as error:
                    raise CasoAsignadoAOtro from error
                ids = await tx.ids_evidencias(caso.id)
                if not set(normalizada.evidencias) <= ids:
                    raise SolicitudInvalida(["evidenciasConsideradas"])
                resultado, marca = await self._escribir(
                    tx,
                    caso,
                    destino,
                    normalizada,
                    solicitud,
                    actor,
                    analista_id,
                    clave,
                    huella,
                    ahora,
                )
                await tx.guardar_idempotencia(clave_guardada, huella, resultado.a_json(), ahora)
        except DecisionFinalDuplicada:
            # Perdió la carrera contra otra decisión final: la base la rechazó y no quedó nada escrito.
            async with self._repo.transaccion() as tx:
                actual = await tx.siniestro(siniestro_id)
            raise TransicionNoPermitida(
                (actual.estado if actual else EstadoSiniestro.APROBADO).value
            ) from None
        return resultado, marca

    async def _escribir(
        self,
        tx: TxSiniestros,
        caso: Siniestro,
        destino: EstadoSiniestro,
        n: _Normalizada,
        solicitud: Solicitud,
        actor: Actor,
        analista_id: UUID,
        clave: UUID,
        huella: str,
        ahora: datetime,
    ) -> tuple[ResultadoDecision, str]:
        decision_id = uuid4()
        referencia: str | None = None
        vence: datetime | None = None
        historial: dict[str, Any]
        detalle: dict[str, Any]
        emitir: Callable[[], Awaitable[None]]

        if isinstance(solicitud, SolicitudAprobar):
            liquidacion = self._liquidacion(caso, solicitud)
            beneficiario = caso.copia_poliza.beneficiario
            if beneficiario is None:
                raise SolicitudInvalida(["beneficiario"])
            nuevo = aprobar(caso, liquidacion, ahora)
            referencia = referencia_pago(caso.codigo)
            valor = f"{liquidacion.valor_indemnizar:f}"
            accion, marca, tipo = ACCION_APROBADO, RESULTADO_APROBADO, "APROBAR"
            historial = {
                "valorIndemnizar": valor,
                "moneda": liquidacion.moneda,
                "referenciaPago": referencia,
            }
            detalle = {
                "liquidacion": {
                    "valorReclamado": f"{liquidacion.valor_reclamado:f}",
                    "deducible": f"{liquidacion.deducible:f}",
                    "valorIndemnizar": valor,
                    "moneda": liquidacion.moneda,
                },
                "referenciaPago": referencia,
            }
            emitir = partial(tx.evento_aprobado, nuevo, referencia, analista_id, ahora)

        elif isinstance(solicitud, SolicitudRechazar):
            causal = CausalRechazo(n.cuerpo["causal"])
            nuevo = rechazar(caso, ahora)
            accion, marca, tipo = ACCION_RECHAZADO, RESULTADO_RECHAZADO, "RECHAZAR"
            historial = {"causal": causal.value}
            detalle = {"causal": causal.value, "explicacionCliente": n.cuerpo["explicacionCliente"]}

            emitir = partial(tx.evento_rechazado, nuevo, causal, analista_id, ahora)

        else:
            documentos = validar_documentos(solicitud.documentos)
            nuevo = solicitar_soporte(caso, solicitud.plazo_dias_habiles, ahora)
            vence = nuevo.plazo_soporte_vence_en
            assert vence is not None  # lo fija solicitar_soporte
            accion, marca, tipo = ACCION_SOPORTE_SOLICITADO, RESULTADO_SOPORTE, "SOLICITAR_SOPORTE"
            historial = {
                "documentos": [d.value for d in documentos],
                "canal": solicitud.canal.value,
                "plazoDiasHabiles": solicitud.plazo_dias_habiles,
                "plazoVenceEn": vence.isoformat(),
            }
            detalle = {**historial, "mensajeCliente": n.cuerpo["mensajeCliente"]}

            emitir = partial(
                tx.evento_soporte_solicitado, nuevo, documentos, solicitud.canal, analista_id, ahora
            )

        await tx.guardar_transicion(nuevo, caso.version)
        await tx.agregar_historial(
            EntradaHistorial(
                id=uuid4(),
                siniestro_id=caso.id,
                accion=accion,
                ocurrido_en=ahora,
                estado_anterior=caso.estado,
                estado_nuevo=destino,
                actor_id=actor.id,
                actor_rol=actor.rol,
                actor_nombre=actor.nombre,
                justificacion=n.justificacion,
                evidencias_consideradas=n.evidencias,
                detalles=historial,
            )
        )
        await tx.registrar_decision(
            DecisionRegistrada(
                id=decision_id,
                siniestro_id=caso.id,
                tipo=tipo,
                idempotency_key=clave,
                huella=huella,
                detalle=detalle,
                actor_id=actor.id,
                registrada_en=ahora,
            )
        )
        await emitir()
        return (
            ResultadoDecision(caso.id, destino, decision_id, referencia, vence, ahora),
            marca,
        )

    @staticmethod
    def _liquidacion(caso: Siniestro, solicitud: SolicitudAprobar) -> Liquidacion:
        copia = caso.copia_poliza
        amparo = next((a for a in copia.amparos if a.codigo == caso.causa), None)
        if amparo is None:
            # La copia no trae el amparo reclamado: no hay suma asegurada contra la que validar.
            raise ValorFueraDeRango(["liquidacion.valorIndemnizar"])
        try:
            return validar_liquidacion(
                valor_reclamado=solicitud.valor_reclamado,
                deducible=solicitud.deducible,
                valor_indemnizar=solicitud.valor_indemnizar,
                moneda=solicitud.moneda.strip(),
                suma_asegurada=amparo.suma_asegurada,
                moneda_poliza=copia.moneda,
                valor_reclamado_registrado=caso.valor_reclamado,
                moneda_registrada=caso.moneda,
            )
        except dominio.ValorFueraDeRango as error:
            raise _traducir(error) from error
