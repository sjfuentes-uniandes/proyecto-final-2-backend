"""Rutas de la decisión del analista (HU-W20, W20-B08). Los decoradores, las firmas y los docstrings
son el contrato (`tests/contract/test_openapi.py`): vienen del prototipo de B1.

Seguridad y errores como en el resto de `siniestros` (DS3-09, `RutaB2`). Log de cierre: solo
identificadores y códigos; **nunca** la justificación, la explicación ni el mensaje al cliente, la
liquidación, nombres ni cuentas.

`GET /catalogos-decision` se registra **antes** de `GET /{siniestroId}` (main.py incluye este router
primero): si no, `catalogos-decision` se leería como un identificador.
"""

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from solventa_common.errors import RutaB2

from siniestros.adapters.inbound.http.dto import (
    CatalogosDecision,
    ResultadoDecisionSiniestro,
    SolicitudAprobacion,
    SolicitudDecision,
    SolicitudRechazo,
)
from siniestros.adapters.inbound.http.siniestros import (
    ActorDep,
    IdSiniestro,
    ServiciosDep,
    _error,
    _exigir_analista,
    _uuid_del_actor,
)
from siniestros.application.siniestro.decidir import (
    ResultadoDecision,
    Solicitud,
    SolicitudAprobar,
    SolicitudRechazar,
    SolicitudSoportar,
)
from siniestros.application.siniestro.vistas import RecursoSiniestro
from siniestros.domain.siniestro.modelos import (
    CanalSoporte,
    CausalRechazo,
    SoporteSolicitable,
)
from siniestros.domain.siniestro.transiciones import DIAS_PLAZO_SOPORTE

logger = logging.getLogger("siniestros.http")

# El `tipo` que Pydantic antepone a la ruta de un campo inválido (unión discriminada): no es parte
# del campo y el portal necesita el nombre limpio para marcarlo.
_ETIQUETAS_TIPO = frozenset({"APROBAR", "RECHAZAR", "SOLICITAR_SOPORTE"})
_CAMPO_TIPO = "tipo"


def _limpiar_campo(campo: str) -> str:
    partes = campo.split(".")
    if partes[0] in _ETIQUETAS_TIPO and len(partes) > 1:
        return ".".join(partes[1:])
    # Sin `tipo`, con uno desconocido o con un cuerpo que no es un objeto, el error cae en el cuerpo.
    return _CAMPO_TIPO if campo == "body" else campo


class RutaDecision(RutaB2):
    """`RutaB2` que además devuelve los campos inválidos sin la etiqueta del `tipo` (R-64)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def manejar(request: Request) -> Response:
            respuesta = await original(request)
            if respuesta.status_code != 422:
                return respuesta
            cuerpo = json.loads(bytes(respuesta.body))
            campos = cuerpo.get("detalles", {}).get("campos")
            if cuerpo.get("codigo") != "SOLICITUD_INVALIDA" or not isinstance(campos, list):
                return respuesta
            cuerpo["detalles"]["campos"] = list(dict.fromkeys(_limpiar_campo(c) for c in campos))
            return JSONResponse(cuerpo, status_code=422)

        return manejar


router = APIRouter(
    prefix="/v1/siniestros",
    tags=["siniestros"],
    route_class=RutaDecision,
    responses={
        401: _error("ACTOR_REQUERIDO: falta X-Actor-Id"),
        403: _error(
            "ROL_NO_AUTORIZADO: X-Actor-Rol falta o no es ANALISTA_SINIESTROS (se audita con "
            "AccesoSiniestroDenegado)"
        ),
    },
)


def _solicitud_de(cuerpo: SolicitudDecision) -> Solicitud:
    """El cuerpo validado, como lo pide el caso de uso (que revisa el contenido)."""
    pedido = cuerpo.root
    evidencias = tuple(pedido.evidencias_consideradas)
    if isinstance(pedido, SolicitudAprobacion):
        liquidacion = pedido.liquidacion
        return SolicitudAprobar(
            justificacion=pedido.justificacion,
            evidencias_consideradas=evidencias,
            valor_reclamado=liquidacion.valor_reclamado,
            deducible=liquidacion.deducible,
            valor_indemnizar=liquidacion.valor_indemnizar,
            moneda=liquidacion.moneda,
        )
    if isinstance(pedido, SolicitudRechazo):
        return SolicitudRechazar(
            justificacion=pedido.justificacion,
            evidencias_consideradas=evidencias,
            causal=pedido.causal,
            explicacion_cliente=pedido.explicacion_cliente,
        )
    return SolicitudSoportar(
        justificacion=pedido.justificacion,
        evidencias_consideradas=evidencias,
        documentos=tuple(pedido.documentos),
        mensaje_cliente=pedido.mensaje_cliente,
        canal=pedido.canal,
        plazo_dias_habiles=pedido.plazo_dias_habiles,
    )


def resultado_dto(resultado: ResultadoDecision) -> ResultadoDecisionSiniestro:
    return ResultadoDecisionSiniestro(
        siniestro_id=resultado.siniestro_id,
        estado_nuevo=resultado.estado_nuevo,
        decision_id=resultado.decision_id,
        referencia_pago=resultado.referencia_pago,
        plazo_soporte_vence_en=resultado.plazo_soporte_vence_en,
        registrada_en=resultado.registrada_en,
    )


@router.get("/catalogos-decision", response_model=CatalogosDecision)
async def catalogos_decision(actor: ActorDep, casos: ServiciosDep) -> CatalogosDecision:
    """Causales de rechazo, documentos solicitables, canales y plazos, como códigos que el portal
    traduce (DS3-12)."""
    await _exigir_analista(casos, actor, RecursoSiniestro.SINIESTRO)
    return CatalogosDecision(
        causales=list(CausalRechazo),
        documentos=list(SoporteSolicitable),
        canales=list(CanalSoporte),
        plazos_dias_habiles=list(DIAS_PLAZO_SOPORTE),
    )


@router.post(
    "/{siniestroId}/decision",
    response_model=ResultadoDecisionSiniestro,
    responses={
        404: _error("SINIESTRO_NO_ENCONTRADO"),
        409: _error(
            "TRANSICION_NO_PERMITIDA (detalles.estadoActual; también si otra decisión ganó la "
            "carrera) o CASO_ASIGNADO_A_OTRO"
        ),
        422: _error(
            "JUSTIFICACION_REQUERIDA, VALOR_FUERA_DE_RANGO (detalles.campos), CAUSAL_INVALIDA, "
            "SOLICITUD_INVALIDA (detalles.campos) o IDEMPOTENCIA_CONFLICTO"
        ),
    },
)
async def decidir(
    actor: ActorDep,
    casos: ServiciosDep,
    siniestro_id: IdSiniestro,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    solicitud: SolicitudDecision,
) -> ResultadoDecisionSiniestro:
    """Registra la decisión del analista asignado sobre un caso EN_EVALUACION: aprobar (con
    liquidación), rechazar (con causal y explicación) o solicitar soporte (FALTA_SOPORTE, reloj en
    pausa). En una sola transacción escribe la decisión, el cambio de estado, el historial inmutable
    y el evento en el Outbox. La misma Idempotency-Key con el mismo cuerpo devuelve el resultado
    original; con otro cuerpo, 422 IDEMPOTENCIA_CONFLICTO."""
    await _exigir_analista(casos, actor, RecursoSiniestro.SINIESTRO, siniestro_id)
    resultado = await casos.decidir.ejecutar(
        siniestro_id, actor, _uuid_del_actor(actor), idempotency_key, _solicitud_de(solicitud)
    )
    logger.info(
        "siniestro decidido",
        extra={
            "siniestro_id": str(siniestro_id),
            "operacion": "siniestro.decidir",
            "resultado": "ok",
            "tipo": solicitud.root.tipo,
            "estado_nuevo": resultado.estado_nuevo.value,
        },
    )
    return resultado_dto(resultado)
