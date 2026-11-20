"""HU-W19 y HU-W20: siniestros en el portal (grupo `analistas-siniestros`).

Rutas espejo de `siniestros` (bandeja, resumen, siguiente, detalle y enlace) con los mismos DTO
(`siniestros_dto.py`) más dos vistas: `clienteNombre`, desde `clientes`, y `polizaVista`, desde
`consulta`. Ambos son null si la dependencia no responde a tiempo (CF-S3-09).

Un usuario sin el grupo recibe 403 y `bff-web` avisa a siniestros sin esperar (DS3-09). El cuerpo
de siniestros se valida contra el DTO antes de devolverlo: si no lo cumple, 503 (falla cerrada). La
URL prefirmada del enlace no se registra.
"""

import json
from collections.abc import Callable, Coroutine
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError
from solventa_common.errors import ErrorNegocio, RutaB2

from bff_web.adapters.inbound.autorizacion import requerir_grupo
from bff_web.adapters.inbound.pago_siniestro_dto import PagoSiniestroVista
from bff_web.adapters.inbound.polizas import PatronNumero, VigenciaPolizaDto
from bff_web.adapters.inbound.siniestros_dto import (
    CatalogosDecision,
    DetalleSiniestro,
    EnlaceEvidencia,
    EstadoSiniestro,
    OrigenSiniestro,
    PrioridadSiniestro,
    ResultadoDecisionSiniestro,
    ResumenBandejaSiniestros,
    ResumenSiniestro,
    SolicitudDecision,
    TextoDecimal,
    TipoBandeja,
    _Dto,
)
from bff_web.adapters.inbound.socios import ErrorDto
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.consulta import ConsultaCliente
from bff_web.adapters.outbound.siniestros import SiniestrosCliente
from bff_web.application.siniestros import (
    Analista,
    Siniestros,
    nombre_de_analista,
    servicio_no_disponible,
)
from bff_web.config import settings


class PolizaVistaDto(_Dto):
    """Datos de la póliza para el encabezado del caso, desde `consulta`."""

    poliza_id: UUID
    numero: PatronNumero
    estado: str
    ramo: str
    producto_nombre: str
    vigencia: VigenciaPolizaDto
    vigente: bool
    suma_asegurada: TextoDecimal
    moneda: str = Field(pattern=r"^[A-Z]{3}$")


class ResumenSiniestroVista(ResumenSiniestro):
    """La fila de siniestros más el nombre del cliente; null si clientes no responde a tiempo."""

    cliente_nombre: str | None


class BandejaSiniestrosVista(_Dto):
    items: list[ResumenSiniestroVista] = Field(
        description="MIA: menor tiempo restante primero. SIN_ASIGNAR: el más antiguo primero"
    )
    siguiente_cursor: str | None
    total: int = Field(description="Casos que cumplen los filtros")


class DetalleSiniestroVista(DetalleSiniestro):
    """El detalle de siniestros más el nombre del cliente y la póliza (null si fallan)."""

    cliente_nombre: str | None
    poliza_vista: PolizaVistaDto | None


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


@lru_cache
def siniestros_servicio() -> Siniestros:
    return Siniestros(
        SiniestrosCliente(settings.siniestros_url, settings.siniestros_timeout_ms),
        ClientesCliente(settings.clientes_url, settings.clientes_timeout_ms),
        ConsultaCliente(settings.consulta_url, settings.consulta_timeout_ms),
        settings.nombre_cliente_timeout_ms,
        settings.consulta_timeout_ms,
        settings.acceso_denegado_timeout_ms,
        settings.compromiso_siniestros_horas,
    )


Servicio = Annotated[Siniestros, Depends(siniestros_servicio)]


async def exigir_grupo_analistas(
    request: Request,
    servicio: Servicio,
    x_authenticated_groups: Annotated[str | None, Header()] = None,
    x_authenticated_issuer: Annotated[str | None, Header()] = None,
    x_authenticated_sub: Annotated[
        str | None, Header(alias="X-Authenticated-Sub", include_in_schema=False)
    ] = None,
) -> None:
    """Exige el grupo; si falta, avisa a siniestros (DS3-09) y responde 403. Es `async` para que el
    aviso se programe en el bucle de la solicitud (una dependencia síncrona corre en un hilo)."""
    try:
        requerir_grupo(settings.grupo_analistas_siniestros)(
            x_authenticated_groups, x_authenticated_issuer
        )
    except HTTPException:
        ruta = request.scope.get("route")
        servicio.avisar_acceso_denegado(
            x_authenticated_sub,
            getattr(ruta, "name", None),
            request.path_params.get("siniestroId"),
        )
        raise


def analista(
    x_authenticated_sub: Annotated[
        str | None, Header(alias="X-Authenticated-Sub", include_in_schema=False)
    ] = None,
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> Analista:
    """El analista autenticado: API Gateway pone su `sub` en X-Authenticated-Sub (DT-17)."""
    if not x_authenticated_sub:
        raise ErrorNegocio("ACTOR_REQUERIDO", "No se identificó al usuario autenticado", 401)
    return Analista(x_authenticated_sub, nombre_de_analista(x_authenticated_sub, authorization))


# El `tipo` que Pydantic antepone a la ruta de un campo inválido (unión discriminada): no es parte
# del campo y el portal necesita el nombre limpio para marcarlo (R-64). Es la misma regla de
# `siniestros`; se repite aquí a propósito (el BFF no importa de otro servicio).
_ETIQUETAS_TIPO = frozenset({"APROBAR", "RECHAZAR", "SOLICITAR_SOPORTE"})


def _limpiar_campo(campo: str) -> str:
    partes = campo.split(".")
    if partes[0] in _ETIQUETAS_TIPO and len(partes) > 1:
        return ".".join(partes[1:])
    return "tipo" if campo == "body" else campo


class RutaDecision(RutaB2):
    """`RutaB2` que además devuelve los campos inválidos sin la etiqueta del `tipo`."""

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


_RESPUESTAS: dict[int | str, dict[str, Any]] = {
    401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
    403: _error(
        "ROL_NO_AUTORIZADO: falta el grupo analistas-siniestros o no es del back-office "
        "(se notifica a siniestros, DS3-09)"
    ),
    422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
    503: _error("SERVICIO_NO_DISPONIBLE: siniestros no respondió o respondió fuera de su contrato"),
}
router = APIRouter(
    prefix="/v1/siniestros",
    tags=["siniestros"],
    route_class=RutaB2,
    dependencies=[Depends(exigir_grupo_analistas)],
    responses=_RESPUESTAS,
)
# La decisión (HU-W20): mismas reglas y respuestas, pero sus 422 salen con los campos sin la etiqueta
# del `tipo`. Se incluye antes que `router`: `/catalogos-decision` va antes de `/{siniestroId}`.
router_decision = APIRouter(
    prefix="/v1/siniestros",
    tags=["siniestros"],
    route_class=RutaDecision,
    dependencies=[Depends(exigir_grupo_analistas)],
    responses=_RESPUESTAS,
)
Actor = Annotated[Analista, Depends(analista)]
IdSiniestro = Annotated[UUID, Path(alias="siniestroId")]
IdEvidencia = Annotated[UUID, Path(alias="evidenciaId")]

_BANDEJA = TypeAdapter(BandejaSiniestrosVista)
_RESUMEN = TypeAdapter(ResumenBandejaSiniestros)
_CASO = TypeAdapter(ResumenSiniestroVista)
_DETALLE = TypeAdapter(DetalleSiniestroVista)
_ENLACE = TypeAdapter(EnlaceEvidencia)
_CATALOGOS = TypeAdapter(CatalogosDecision)
_RESULTADO = TypeAdapter(ResultadoDecisionSiniestro)
_POLIZA = TypeAdapter(PolizaVistaDto)
_PAGO = TypeAdapter(PagoSiniestroVista)


def _validar(adaptador: TypeAdapter[Any], cuerpo: Any) -> Any:
    """El cuerpo de siniestros, si cumple el DTO; si no, 503."""
    try:
        adaptador.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


@router.get("", response_model=BandejaSiniestrosVista)
async def bandeja(
    servicio: Servicio,
    analista: Actor,
    bandeja: Annotated[TipoBandeja, Query(description="MIA: asignados al actor")] = TipoBandeja.MIA,
    estado: EstadoSiniestro | None = None,
    origen: OrigenSiniestro | None = None,
    prioridad: PrioridadSiniestro | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Any:
    """Bandeja del analista (H01) o cola general (H02), con el nombre del cliente. `q` busca por
    código de siniestro o por número de póliza (CF-S3-06)."""
    parametros: dict[str, Any] = {"bandeja": bandeja.value, "limite": limite}
    for nombre, valor in (("estado", estado), ("origen", origen), ("prioridad", prioridad)):
        if valor is not None:
            parametros[nombre] = valor.value
    if q is not None:
        parametros["q"] = q
    if cursor is not None:
        parametros["cursor"] = cursor
    return _validar(_BANDEJA, await servicio.bandeja(parametros, analista))


@router.get(
    "/resumen",
    response_model=ResumenBandejaSiniestros,
    responses={422: _error("SOLICITUD_INVALIDA (también si desde ≥ hasta)")},
)
async def resumen(
    servicio: Servicio,
    analista: Actor,
    desde: Annotated[AwareDatetime, Query(description="Inicio (incluido), ISO-8601 con zona")],
    hasta: Annotated[AwareDatetime, Query(description="Fin (excluido), ISO-8601 con zona")],
) -> Any:
    """Indicadores de la bandeja; `desde` y `hasta` delimitan el día del usuario."""
    parametros = {"desde": desde.isoformat(), "hasta": hasta.isoformat()}
    return _validar(_RESUMEN, await servicio.resumen(parametros, analista))


@router.post(
    "/siguiente",
    response_model=ResumenSiniestroVista,
    responses={
        404: _error("COLA_SIN_CASOS: no hay siniestros REPORTADOS sin analista (estado H02)"),
        422: _error("SOLICITUD_INVALIDA o IDEMPOTENCIA_CONFLICTO"),
    },
)
async def tomar_siguiente(
    servicio: Servicio,
    analista: Actor,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> Any:
    """Asigna al analista el caso más antiguo de la cola. La misma Idempotency-Key devuelve el
    mismo caso."""
    return _validar(_CASO, await servicio.tomar_siguiente(idempotency_key, analista))


@router.get(
    "/{siniestroId}",
    response_model=DetalleSiniestroVista,
    responses={404: _error("SINIESTRO_NO_ENCONTRADO")},
)
async def detalle(servicio: Servicio, analista: Actor, siniestro_id: IdSiniestro) -> Any:
    """Detalle del caso (H03, H04, Cobertura e Historial). Una evidencia cuyo objeto no existe
    viene como NO_DISPONIBLE y no impide la respuesta (AC4). Abrir un caso no lo asigna."""
    caso = await servicio.detalle(siniestro_id, analista)
    if isinstance(caso, dict) and caso.get("polizaVista") is not None:
        try:
            _POLIZA.validate_python(caso["polizaVista"])
        except ValidationError:
            caso["polizaVista"] = None
    return _validar(_DETALLE, caso)


@router.get(
    "/{siniestroId}/pago",
    response_model=PagoSiniestroVista,
    responses={
        404: _error(
            "SINIESTRO_NO_ENCONTRADO, o INDEMNIZACION_NO_ENCONTRADA si el caso existe pero no "
            "está APROBADO (no hay pago ordenado)"
        ),
    },
)
async def pago(servicio: Servicio, analista: Actor, siniestro_id: IdSiniestro) -> Any:
    """Pago del siniestro aprobado (H08): valor, intentos, pagos duplicados descartados,
    confirmación de la pasarela y trazabilidad del trámite. Plan B: se arma solo con `siniestros`;
    la `indemnizacion` es null mientras `dinero` no haya publicado un resultado."""
    return _validar(_PAGO, await servicio.pago(siniestro_id, analista))


@router.post(
    "/{siniestroId}/evidencias/{evidenciaId}/enlace",
    response_model=EnlaceEvidencia,
    responses={
        404: _error(
            "SINIESTRO_NO_ENCONTRADO, EVIDENCIA_NO_ENCONTRADA o EVIDENCIA_NO_DISPONIBLE (el "
            "objeto no existe: no se emite la URL)"
        ),
    },
)
async def emitir_enlace(
    servicio: Servicio, analista: Actor, siniestro_id: IdSiniestro, evidencia_id: IdEvidencia
) -> Any:
    """URL prefirmada de lectura (≤ 300 s): el portal la pide al abrir el visor y no la guarda."""
    return _validar(_ENLACE, await servicio.enlace(siniestro_id, evidencia_id, analista))


@router_decision.get("/catalogos-decision", response_model=CatalogosDecision)
async def catalogos_decision(servicio: Servicio, analista: Actor) -> Any:
    """Causales de rechazo, documentos solicitables, canales y plazos, como códigos que el portal
    traduce (DS3-12)."""
    return _validar(_CATALOGOS, await servicio.catalogos(analista))


@router_decision.post(
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
    servicio: Servicio,
    analista: Actor,
    siniestro_id: IdSiniestro,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    solicitud: SolicitudDecision,
) -> Any:
    """Registra la decisión del analista asignado sobre un caso EN_EVALUACION: aprobar (con
    liquidación), rechazar (con causal y explicación) o solicitar soporte (FALTA_SOPORTE, reloj en
    pausa). La misma Idempotency-Key con el mismo cuerpo devuelve el resultado original; con otro
    cuerpo, 422 IDEMPOTENCIA_CONFLICTO."""
    cuerpo = solicitud.model_dump(mode="json", by_alias=True)
    return _validar(
        _RESULTADO, await servicio.decidir(siniestro_id, idempotency_key, cuerpo, analista)
    )
