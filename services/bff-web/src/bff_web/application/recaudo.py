"""HU-W16 (16.14): estado del recaudo de la prima aceptada, para el paso 3 del portal. Reenvía a
polizas, de solo lectura.

Falla cerrada, como la aceptación: solo pasan, con su estado y su cuerpo, los 4xx que el contrato
de polizas promete para esta ruta. Cualquier otra respuesta (un 5xx, también un 503
DEPENDENCIA_NO_DISPONIBLE, otro código, un timeout, un error de conexión o un cuerpo que no es JSON)
es 503 SERVICIO_NO_DISPONIBLE sin detalles. El cuerpo 200 lo valida contra el DTO la ruta.
"""

from typing import Any
from uuid import UUID

from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.suscripcion import proteger

CODIGOS_RECAUDO = frozenset(
    {
        "ACTOR_REQUERIDO",
        "ROL_NO_AUTORIZADO",
        "SOLICITUD_INVALIDA",
        "ACEPTACION_NO_ENCONTRADA",
    }
)
_SIN_5XX: frozenset[str] = frozenset()


class Recaudos:
    def __init__(self, polizas: PolizasCliente) -> None:
        self.polizas = polizas

    async def consultar(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        with operacion("recaudo.consultar") as op:
            try:
                estado: dict[str, Any] = await proteger(
                    self.polizas.recaudo(cotizacion_id, actor), CODIGOS_RECAUDO, _SIN_5XX
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
        return estado
