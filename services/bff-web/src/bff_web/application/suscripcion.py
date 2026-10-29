"""HU-W13: suscripción automática desde el portal (E01 a E04). Reenvía a polizas y compone el
nombre del cliente desde clientes.

Falla cerrado, como la lectura de consentimientos (HU-W31):
- Solo pasan, con su estado y su cuerpo, los errores que el contrato de polizas promete.
- Cualquier otra respuesta (otro código, cualquier 5xx salvo DEPENDENCIA_NO_DISPONIBLE, un timeout,
  un error de conexión o un cuerpo que no es JSON) es 503 SERVICIO_NO_DISPONIBLE. El cuerpo 200
  lo valida contra el DTO la ruta, que lo publica.

El nombre del cliente (E01, E03, E04) se pide a clientes en paralelo y con su propio timeout; si
falla, la vista sale con `clienteNombre = null` y no se rompe. DS2-07: para un actor que no es
suscriptor, las reglas RESERVADA se devuelven sin `valorCaso` aunque polizas no las haya filtrado.
"""

from collections.abc import Awaitable
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import ROL_SUSCRIPTOR, ErrorPolizas, PolizasCliente
from bff_web.application.nombres import nombres_de_clientes

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
CODIGOS_POLIZAS = frozenset(
    {
        "ACTOR_REQUERIDO",
        "ROL_NO_AUTORIZADO",
        "SOLICITUD_INVALIDA",
        "IDEMPOTENCIA_CONFLICTO",
        "COTIZACION_NO_ENCONTRADA",
        "DECISION_NO_ENCONTRADA",
        "IDENTIDAD_NO_VERIFICADA",
        "IDENTIDAD_PENDIENTE",
        "OFERTA_VENCIDA",
        "REGLAS_NO_VIGENTES",
    }
)
# 5xx de polizas que el portal sabe explicar (detalles.dependencia: cotizacion o catalogo).
_REENVIADOS_5XX = frozenset({"DEPENDENCIA_NO_DISPONIBLE"})


def servicio_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(
        SERVICIO_NO_DISPONIBLE, "El servicio de suscripción no está disponible", 503
    )


async def _proteger(llamada: Awaitable[Any]) -> Any:
    return await proteger(llamada, CODIGOS_POLIZAS, _REENVIADOS_5XX)


async def proteger(
    llamada: Awaitable[Any], codigos: frozenset[str], reenviados_5xx: frozenset[str]
) -> Any:
    """Falla cerrada (también la usa la aceptación, HU-W15): solo pasan los 4xx de `codigos` y
    los 503 de `reenviados_5xx`; todo lo demás es 503 SERVICIO_NO_DISPONIBLE."""
    try:
        return await llamada
    except ErrorPolizas as error:
        codigo = error.cuerpo.get("codigo")
        prometido = (error.status < 500 and codigo in codigos) or (
            error.status == 503 and codigo in reenviados_5xx
        )
        if not prometido:
            raise servicio_no_disponible() from error
        detalles = error.cuerpo.get("detalles")
        raise ErrorNegocio(
            str(codigo),
            str(error.cuerpo.get("mensaje") or "Solicitud rechazada"),
            error.status,
            detalles if isinstance(detalles, dict) else {},
        ) from error
    except (httpx.HTTPError, ValueError) as error:
        raise servicio_no_disponible() from error


def sin_reservadas(decision: dict[str, Any]) -> dict[str, Any]:
    """DS2-07 en el BFF: valorCaso nulo y reservada en las reglas RESERVADA."""
    reglas = [
        {**regla, "valorCaso": None, "reservada": True}
        if regla.get("visibilidad") == "RESERVADA"
        else regla
        for regla in decision.get("reglasEvaluadas", [])
    ]
    return {**decision, "reglasEvaluadas": reglas}


class Suscripciones:
    def __init__(
        self, polizas: PolizasCliente, clientes: ClientesCliente, nombre_timeout_ms: int
    ) -> None:
        self.polizas = polizas
        self.clientes = clientes
        self._nombre_timeout_s = nombre_timeout_ms / 1000

    async def solicitar(
        self, cuerpo: dict[str, Any], actor: str, clave: UUID
    ) -> tuple[int, dict[str, Any]]:
        with operacion("suscripcion.solicitar") as op:
            try:
                estado, decision = await _proteger(self.polizas.solicitar(cuerpo, actor, clave))
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
        return estado, sin_reservadas(decision)

    async def decision(self, decision_id: UUID, actor: str, rol: str) -> dict[str, Any]:
        """La decisión con el nombre del cliente (E02, E03, E04)."""
        with operacion("suscripcion.consultar") as op:
            try:
                decision = await _proteger(self.polizas.decision(decision_id, actor, rol))
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            if not isinstance(decision, dict):
                raise servicio_no_disponible()
            nombres = await self._nombres([decision.get("clienteId")], actor)
        vista = decision if rol == ROL_SUSCRIPTOR else sin_reservadas(decision)
        return {**vista, "clienteNombre": nombres.get(str(decision.get("clienteId")))}

    async def por_cotizacion(self, cotizacion_id: UUID, actor: str) -> dict[str, Any]:
        with operacion("suscripcion.consultar"):
            lista: dict[str, Any] = await _proteger(
                self.polizas.por_cotizacion(cotizacion_id, actor)
            )
        return lista

    async def bandeja(self, parametros: dict[str, Any], actor: str) -> dict[str, Any]:
        with operacion("suscripcion.bandeja") as op:
            try:
                bandeja = await _proteger(self.polizas.bandeja(parametros, actor))
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            if not isinstance(bandeja, dict) or not isinstance(bandeja.get("items"), list):
                raise servicio_no_disponible()
            casos = [c for c in bandeja["items"] if isinstance(c, dict)]
            nombres = await self._nombres([c.get("clienteId") for c in casos], actor)
        items = [{**c, "clienteNombre": nombres.get(str(c.get("clienteId")))} for c in casos]
        return {**bandeja, "items": items}

    async def resumen(self, parametros: dict[str, Any], actor: str) -> dict[str, Any]:
        with operacion("suscripcion.resumen") as op:
            try:
                resumen: dict[str, Any] = await _proteger(self.polizas.resumen(parametros, actor))
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
        return resumen

    async def _nombres(self, clientes: list[Any], actor: str) -> dict[str, str | None]:
        """Nombre de cada cliente distinto, en paralelo; None si clientes no lo da a tiempo."""
        return await nombres_de_clientes(self.clientes, clientes, actor, self._nombre_timeout_s)
