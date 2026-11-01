"""HU-W17 (17.11): la póliza de la oferta aceptada y la URL de su documento, para el portal.
Reenvía a polizas, de solo lectura.

Falla cerrada, como el recaudo: solo pasan, con su estado y su cuerpo, los 4xx que el contrato de
polizas promete para **cada** ruta. Cualquier otra respuesta (un 5xx, también un 503
DEPENDENCIA_NO_DISPONIBLE, otro código, un timeout, un error de conexión o un cuerpo que no es JSON)
es 503 SERVICIO_NO_DISPONIBLE sin detalles. El cuerpo 200 lo valida contra el DTO la ruta.

La URL prefirmada no se registra: ni la medición ni los errores llevan el cuerpo.

17.11c: la póliza agrega `titularNombre`, que se pide a clientes con el `clienteId` de polizas, como
el `clienteNombre` de la suscripción (`nombres_de_clientes`, con su timeout). Solo con la póliza
ACTIVA: mientras se emite, el portal la consulta en bucle y no se llama a clientes. Si clientes
falla, tarda o no tiene el nombre, `titularNombre` es None y la póliza sale igual. El `clienteId` no
llega al portal: el DTO de la ruta no lo tiene.
"""

from typing import Any
from uuid import UUID

from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.nombres import nombres_de_clientes
from bff_web.application.suscripcion import proteger, servicio_no_disponible

ACTIVA = "ACTIVA"
_COMUNES = frozenset({"ACTOR_REQUERIDO", "ROL_NO_AUTORIZADO", "SOLICITUD_INVALIDA"})
CODIGOS_POLIZA = _COMUNES | {"ACEPTACION_NO_ENCONTRADA", "POLIZA_NO_ENCONTRADA"}
CODIGOS_DOCUMENTO = _COMUNES | {"POLIZA_NO_ENCONTRADA", "POLIZA_NO_ACTIVA"}
_SIN_5XX: frozenset[str] = frozenset()


class Polizas:
    def __init__(
        self, polizas: PolizasCliente, clientes: ClientesCliente, nombre_timeout_ms: int
    ) -> None:
        self.polizas = polizas
        self.clientes = clientes
        self._nombre_timeout_s = nombre_timeout_ms / 1000

    async def consultar(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        with operacion("poliza.consultar") as op:
            try:
                poliza = await proteger(
                    self.polizas.poliza(cotizacion_id, actor), CODIGOS_POLIZA, _SIN_5XX
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            if not isinstance(poliza, dict):
                raise servicio_no_disponible()
            cliente_id = poliza.get("clienteId")
            nombre = None
            if poliza.get("estado") == ACTIVA:
                nombres = await nombres_de_clientes(
                    self.clientes, [cliente_id], actor, self._nombre_timeout_s
                )
                nombre = nombres.get(str(cliente_id))
        return {**poliza, "titularNombre": nombre}

    async def documento(self, poliza_id: UUID, actor: str) -> dict[str, Any]:
        with operacion("poliza.documento") as op:
            try:
                enlace: dict[str, Any] = await proteger(
                    self.polizas.documento(poliza_id, actor), CODIGOS_DOCUMENTO, _SIN_5XX
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
        return enlace
