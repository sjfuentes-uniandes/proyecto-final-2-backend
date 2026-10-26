"""HU-W15: registro de la aceptación de la oferta desde el portal (F01). Reenvía a polizas.

Falla cerrada, como la suscripción: solo pasan, con su estado y su cuerpo, los errores 4xx que el
contrato de polizas promete para la aceptación. Cualquier otra respuesta (un 5xx, otro código, un
timeout o un cuerpo que no es JSON) es 503 SERVICIO_NO_DISPONIBLE: la aceptación no depende de
otros servicios, así que ningún 5xx de polizas se explica al portal.
"""

from typing import Any
from uuid import UUID

from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.suscripcion import proteger

CODIGOS_ACEPTACION = frozenset(
    {
        "ACTOR_REQUERIDO",
        "ROL_NO_AUTORIZADO",
        "SOLICITUD_INVALIDA",
        "IDEMPOTENCIA_CONFLICTO",
        "DECISION_NO_ENCONTRADA",
        "DECISION_NO_APROBADA",
        "OFERTA_VENCIDA",
        "OFERTA_YA_ACEPTADA",
        "DECLARACIONES_INCOMPLETAS",
        "TEXTO_NO_VIGENTE",
        "ACEPTACION_NO_ENCONTRADA",
    }
)
_SIN_5XX: frozenset[str] = frozenset()


class Aceptaciones:
    def __init__(self, polizas: PolizasCliente) -> None:
        self.polizas = polizas

    async def registrar(
        self, cotizacion_id: UUID, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        with operacion("aceptacion.registrar") as op:
            try:
                resultado: tuple[int, dict[str, Any]] = await proteger(
                    self.polizas.aceptar(cotizacion_id, cuerpo, actor, clave),
                    CODIGOS_ACEPTACION,
                    _SIN_5XX,
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
        return resultado

    async def consultar(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        with operacion("aceptacion.consultar") as op:
            try:
                aceptacion: dict[str, Any] = await proteger(
                    self.polizas.aceptacion(cotizacion_id, actor), CODIGOS_ACEPTACION, _SIN_5XX
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
        return aceptacion
