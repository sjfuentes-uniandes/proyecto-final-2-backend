"""HU-W19 y HU-W20: siniestros para el analista (grupo `analistas-siniestros`). Reenvía a `siniestros` y le
agrega lo que no guarda: el nombre del cliente (de `clientes`) y la póliza del encabezado (de
`consulta`).

- **Composición con degradación (CF-S3-09).** El nombre y la póliza se piden en paralelo, con su
  propio timeout y después de que siniestros respondió. Si fallan, la respuesta sale igual con
  `clienteNombre` o `polizaVista` en `null`. El `clienteId` ya viene en el contrato de siniestros.
- **Falla cerrada.** Solo pasan, con su estado, los 404, 409 y 422 del contrato de siniestros (por
  su código). Cualquier otra respuesta (401, 403, 5xx, un timeout, un error de conexión o un cuerpo que
  no es JSON) es 503 `SERVICIO_NO_DISPONIBLE` sin detalles.
- **Acceso denegado (DS3-09).** Cuando la ruta rechaza a un usuario sin el grupo, avisa a siniestros
  sin esperar la respuesta (300 ms, sin reintento). Si falla, deja el log `acceso.denegado` sin
  datos personales.
- **Actor (DS3-10).** El `sub`, y el nombre: el `name` del token, o la parte local del correo, o el
  `sub`. El token ya lo validó API Gateway; aquí solo se lee para mostrar el nombre.
"""

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from solventa_common.errors import ErrorNegocio
from solventa_common.telemetry import RESULTADO_RECHAZADO, operacion

from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.consulta import ConsultaCliente, ErrorConsulta
from bff_web.adapters.outbound.siniestros import ErrorSiniestros, SiniestrosCliente
from bff_web.application.nombres import nombres_de_clientes

logger = logging.getLogger("bff_web.siniestros")

SERVICIO_NO_DISPONIBLE = "SERVICIO_NO_DISPONIBLE"
# Los errores de siniestros que pasan, cada uno con **su** estado: un código con otro estado es un
# cuerpo fuera del contrato y falla cerrado.
CODIGOS_SINIESTROS: dict[str, int] = {
    "SINIESTRO_NO_ENCONTRADO": 404,
    "EVIDENCIA_NO_ENCONTRADA": 404,
    "EVIDENCIA_NO_DISPONIBLE": 404,
    "COLA_SIN_CASOS": 404,
    "SOLICITUD_INVALIDA": 422,
    "IDEMPOTENCIA_CONFLICTO": 422,
    # HU-W20: la decisión del analista.
    "TRANSICION_NO_PERMITIDA": 409,
    "CASO_ASIGNADO_A_OTRO": 409,
    "JUSTIFICACION_REQUERIDA": 422,
    "VALOR_FUERA_DE_RANGO": 422,
    "CAUSAL_INVALIDA": 422,
}
CAMPOS_POLIZA_VISTA = (
    "polizaId",
    "numero",
    "estado",
    "ramo",
    "productoNombre",
    "vigencia",
    "vigente",
    "sumaAsegurada",
    "moneda",
)
MAX_NOMBRE = 80
MOTIVO_SIN_GRUPO = "SIN_GRUPO"
RECURSOS = {
    "bandeja": "BANDEJA",
    "resumen": "RESUMEN",
    "tomar_siguiente": "SIGUIENTE",
    "detalle": "SINIESTRO",
    "emitir_enlace": "EVIDENCIA",
    "decidir": "SINIESTRO",
    "catalogos_decision": "SINIESTRO",
}

# Las tareas de aviso en curso: el bucle solo guarda referencias débiles.
_AVISOS: set["asyncio.Task[None]"] = set()


def servicio_no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "El servicio de siniestros no está disponible", 503)


@dataclass(frozen=True)
class Analista:
    id: str
    nombre: str


def nombre_de_analista(sub: str, autorizacion: str | None) -> str:
    """El `name` del token, o la parte local del correo, o el `sub` (DS3-10)."""
    carga = _carga_del_token(autorizacion)
    nombre = carga.get("name")
    if not isinstance(nombre, str) or not nombre.strip():
        correo = carga.get("email")
        nombre = correo.split("@", 1)[0] if isinstance(correo, str) and "@" in correo else sub
    limpio = re.sub(r"[\x00-\x1f\x7f]", "", nombre).strip()
    return limpio[:MAX_NOMBRE] or sub


def _carga_del_token(autorizacion: str | None) -> dict[str, Any]:
    if not autorizacion:
        return {}
    token = autorizacion.removeprefix("Bearer ").strip()
    partes = token.split(".")
    if len(partes) != 3:
        return {}
    try:
        relleno = "=" * (-len(partes[1]) % 4)
        carga = json.loads(base64.urlsafe_b64decode(partes[1] + relleno))
    except (ValueError, UnicodeDecodeError):
        return {}
    return carga if isinstance(carga, dict) else {}


async def _proteger(llamada: Awaitable[Any]) -> Any:
    try:
        return await llamada
    except ErrorSiniestros as error:
        codigo = error.cuerpo.get("codigo")
        if CODIGOS_SINIESTROS.get(str(codigo)) != error.status:
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


class Siniestros:
    def __init__(
        self,
        siniestros: SiniestrosCliente,
        clientes: ClientesCliente,
        consulta: ConsultaCliente,
        nombre_timeout_ms: int,
        poliza_timeout_ms: int,
        aviso_timeout_ms: int,
    ) -> None:
        self.siniestros = siniestros
        self.clientes = clientes
        self.consulta = consulta
        self._nombre_timeout_s = nombre_timeout_ms / 1000
        self._poliza_timeout_s = poliza_timeout_ms / 1000
        self._aviso_timeout_s = aviso_timeout_ms / 1000

    async def _con_operacion(self, nombre: str, llamada: Awaitable[Any]) -> Any:
        with operacion(nombre) as op:
            try:
                return await _proteger(llamada)
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise

    async def bandeja(self, parametros: dict[str, Any], analista: Analista) -> Any:
        """La página de siniestros con el nombre de cada cliente."""
        with operacion("siniestro.bandeja") as op:
            try:
                pagina = await _proteger(
                    self.siniestros.bandeja(parametros, analista.id, analista.nombre)
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            items = pagina.get("items") if isinstance(pagina, dict) else None
            if not isinstance(items, list):
                return pagina
            nombres = await nombres_de_clientes(
                self.clientes,
                [i.get("clienteId") for i in items if isinstance(i, dict)],
                analista.id,
                self._nombre_timeout_s,
            )
            for item in items:
                if isinstance(item, dict):
                    item["clienteNombre"] = nombres.get(str(item.get("clienteId")))
            return pagina

    async def resumen(self, parametros: dict[str, Any], analista: Analista) -> Any:
        return await self._con_operacion(
            "siniestro.bandeja", self.siniestros.resumen(parametros, analista.id, analista.nombre)
        )

    async def tomar_siguiente(self, clave: UUID, analista: Analista) -> Any:
        with operacion("siniestro.bandeja") as op:
            try:
                caso = await _proteger(
                    self.siniestros.tomar_siguiente(clave, analista.id, analista.nombre)
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            if isinstance(caso, dict):
                nombres = await nombres_de_clientes(
                    self.clientes, [caso.get("clienteId")], analista.id, self._nombre_timeout_s
                )
                caso["clienteNombre"] = nombres.get(str(caso.get("clienteId")))
            return caso

    async def detalle(self, siniestro_id: UUID, analista: Analista) -> Any:
        """El detalle más `clienteNombre` y `polizaVista` (null si clientes o consulta fallan)."""
        with operacion("siniestro.consultar") as op:
            try:
                caso = await _proteger(
                    self.siniestros.detalle(siniestro_id, analista.id, analista.nombre)
                )
            except ErrorNegocio as error:
                if error.estado_http < 500:
                    op.marcar(RESULTADO_RECHAZADO)
                raise
            if not isinstance(caso, dict):
                return caso
            nombres, poliza = await asyncio.gather(
                nombres_de_clientes(
                    self.clientes, [caso.get("clienteId")], analista.id, self._nombre_timeout_s
                ),
                self._poliza_vista(caso.get("clienteId"), caso.get("polizaId"), analista.id),
            )
            caso["clienteNombre"] = nombres.get(str(caso.get("clienteId")))
            caso["polizaVista"] = poliza
            return caso

    async def enlace(self, siniestro_id: UUID, evidencia_id: UUID, analista: Analista) -> Any:
        return await self._con_operacion(
            "evidencia.enlace",
            self.siniestros.enlace(siniestro_id, evidencia_id, analista.id, analista.nombre),
        )

    async def catalogos(self, analista: Analista) -> Any:
        """Las listas cerradas de la decisión (HU-W20), tal como las entrega siniestros."""
        return await _proteger(self.siniestros.catalogos_decision(analista.id, analista.nombre))

    async def decidir(
        self, siniestro_id: UUID, clave: UUID, cuerpo: dict[str, Any], analista: Analista
    ) -> Any:
        """Reenvía la decisión a siniestros con la misma `Idempotency-Key` y el actor (HU-W20). Los
        errores de contenido y de estado de siniestros pasan con su código y su estado. El cuerpo no
        se registra: lleva la explicación y el mensaje al cliente."""
        return await self._con_operacion(
            "siniestro.decidir",
            self.siniestros.decidir(siniestro_id, clave, cuerpo, analista.id, analista.nombre),
        )

    async def _poliza_vista(
        self, cliente_id: Any, poliza_id: Any, actor: str
    ) -> dict[str, Any] | None:
        try:
            poliza = await asyncio.wait_for(
                self.consulta.poliza_de_cliente(UUID(str(cliente_id)), UUID(str(poliza_id)), actor),
                self._poliza_timeout_s,
            )
        except (TimeoutError, ErrorConsulta, httpx.HTTPError, ValueError):
            return None
        if not isinstance(poliza, dict) or not all(c in poliza for c in CAMPOS_POLIZA_VISTA):
            return None
        return {campo: poliza[campo] for campo in CAMPOS_POLIZA_VISTA}

    # --- DS3-09 ------------------------------------------------------------------------------------
    def avisar_acceso_denegado(
        self, actor: str | None, ruta: str | None, siniestro_id: str | None
    ) -> None:
        """Avisa a siniestros del rechazo sin esperar la respuesta. Sin usuario no hay a quién
        atribuirlo: solo queda el log."""
        recurso = RECURSOS.get(ruta or "", "BANDEJA")
        if not actor:
            logger.warning(
                "acceso.denegado", extra={"recurso": recurso, "motivo": MOTIVO_SIN_GRUPO}
            )
            return
        tarea = asyncio.create_task(self._avisar(actor, recurso, siniestro_id))
        _AVISOS.add(tarea)
        tarea.add_done_callback(_AVISOS.discard)

    async def _avisar(self, actor: str, recurso: str, siniestro_id: str | None) -> None:
        try:
            await self.siniestros.acceso_denegado(
                actor, recurso, siniestro_id, MOTIVO_SIN_GRUPO, self._aviso_timeout_s
            )
        except Exception as error:  # noqa: BLE001 - el mensaje puede traer datos
            logger.warning(
                "acceso.denegado",
                extra={
                    "recurso": recurso,
                    "motivo": MOTIVO_SIN_GRUPO,
                    "error_type": type(error).__name__,
                },
            )


async def esperar_avisos() -> None:
    """Para las pruebas: espera los avisos de acceso denegado en curso."""
    if _AVISOS:
        await asyncio.gather(*list(_AVISOS), return_exceptions=True)
