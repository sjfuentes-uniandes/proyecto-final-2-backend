"""Rutas de `siniestros` (HU-W19, W19-B12). Responden sus errores en formato B2 (`RutaB2`).

Seguridad (DS3-09): todas las rutas, salvo `accesos-denegados`, exigen `X-Actor-Rol =
ANALISTA_SINIESTROS`. Si falta o es otro, el servicio escribe `AccesoSiniestroDenegado` en el Outbox
**antes** de responder 403.

Log de cierre: solo identificadores (`siniestro_id`, `operacion`, `resultado`). Nunca la URL
prefirmada, su firma, la clave del objeto, nombres ni cuentas.
"""

import logging
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import AwareDatetime
from solventa_common.errors import RutaB2

from siniestros.adapters.inbound.http.conversion import (
    bandeja_dto,
    detalle_dto,
    enlace_dto,
    indicadores_dto,
    resumen_dto,
)
from siniestros.adapters.inbound.http.dto import (
    BandejaSiniestrosRespuesta,
    DetalleSiniestro,
    EnlaceEvidencia,
    ErrorDto,
    ResumenBandejaSiniestros,
    ResumenSiniestro,
    SolicitudAccesoDenegado,
)
from siniestros.application.siniestro.acceso import RegistrarAccesoDenegado
from siniestros.application.siniestro.bandeja import ConsultarResumen, ListarBandeja
from siniestros.application.siniestro.decidir import Decidir
from siniestros.application.siniestro.detalle import ConsultarDetalle
from siniestros.application.siniestro.enlace import EmitirEnlace
from siniestros.application.siniestro.errores import (
    ActorRequerido,
    DependenciaNoDisponible,
    RolNoAutorizado,
)
from siniestros.application.siniestro.puertos import Reloj
from siniestros.application.siniestro.tomar import TomarSiguiente
from siniestros.application.siniestro.vistas import (
    ROL_ANALISTA,
    AccesoDenegado,
    Actor,
    FiltroBandeja,
    MotivoDenegacion,
    OrigenDenegacion,
    Plazos,
    RecursoSiniestro,
    TipoBandeja,
)
from siniestros.domain.siniestro.modelos import EstadoSiniestro, OrigenSiniestro, PrioridadSiniestro

logger = logging.getLogger("siniestros.http")

MAX_ACTOR = 128
MAX_ROL = 64
RESULTADO_OK = "ok"


@dataclass(frozen=True)
class Servicios:
    """Los casos de uso armados por `crear_app`; las rutas no conocen los adaptadores."""

    bandeja: ListarBandeja
    resumen: ConsultarResumen
    tomar: TomarSiguiente
    detalle: ConsultarDetalle
    enlace: EmitirEnlace
    decidir: Decidir
    acceso: RegistrarAccesoDenegado
    reloj: Reloj
    plazos: Plazos


def servicios(request: Request) -> Servicios:
    resultado: Servicios | None = getattr(request.app.state, "servicios", None)
    if resultado is None:
        # Sin base configurada la app arranca (para el healthcheck) pero no atiende solicitudes.
        raise DependenciaNoDisponible("base de datos")
    return resultado


def _nombre_utf8(valor: str | None) -> str | None:
    """`bff-web` envía el nombre en UTF-8; el servidor HTTP lo entrega leído como latin-1."""
    if valor is None:
        return None
    try:
        return valor.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return valor


def actor_siniestros(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
    x_actor_nombre: Annotated[str | None, Header(alias="X-Actor-Nombre")] = None,
) -> Actor:
    if not x_actor_id or not x_actor_id.strip() or len(x_actor_id) > MAX_ACTOR:
        raise ActorRequerido
    return Actor(x_actor_id.strip(), x_actor_rol, _nombre_utf8(x_actor_nombre))


ActorDep = Annotated[Actor, Depends(actor_siniestros)]
ServiciosDep = Annotated[Servicios, Depends(servicios)]


def _uuid_del_actor(actor: Actor) -> UUID:
    """El `sub` de Cognito es un UUID; lo que no lo sea no puede tener casos asignados."""
    try:
        return UUID(actor.id)
    except ValueError as error:
        raise ActorRequerido from error


async def _exigir_analista(
    servicios_: Servicios, actor: Actor, recurso: RecursoSiniestro, recurso_id: UUID | None = None
) -> None:
    if actor.rol == ROL_ANALISTA:
        return
    await servicios_.acceso.ejecutar(
        AccesoDenegado(
            actor_id=actor.id,
            rol=actor.rol[:MAX_ROL] if actor.rol else None,
            recurso=recurso,
            recurso_id=str(recurso_id) if recurso_id else None,
            motivo=MotivoDenegacion.ROL_AUSENTE if not actor.rol else MotivoDenegacion.ROL_INVALIDO,
            origen=OrigenDenegacion.SINIESTROS,
        )
    )
    raise RolNoAutorizado


def _cierre(mensaje: str, operacion: str, siniestro_id: UUID | None = None) -> None:
    logger.info(
        mensaje,
        extra={
            "siniestro_id": str(siniestro_id) if siniestro_id else None,
            "operacion": operacion,
            "resultado": RESULTADO_OK,
        },
    )


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


router = APIRouter(
    prefix="/v1/siniestros",
    tags=["siniestros"],
    route_class=RutaB2,
    responses={
        401: _error("ACTOR_REQUERIDO: falta X-Actor-Id"),
        403: _error(
            "ROL_NO_AUTORIZADO: X-Actor-Rol falta o no es ANALISTA_SINIESTROS (se audita con "
            "AccesoSiniestroDenegado)"
        ),
    },
)

# DS3-09: solo audita; no valida el rol, así que no declara el 403 de ROL_NO_AUTORIZADO.
router_accesos = APIRouter(
    prefix="/v1/siniestros",
    tags=["siniestros"],
    route_class=RutaB2,
    responses={401: _error("ACTOR_REQUERIDO: falta X-Actor-Id")},
)

IdSiniestro = Annotated[UUID, Path(alias="siniestroId")]
IdEvidencia = Annotated[UUID, Path(alias="evidenciaId")]


@router.get(
    "",
    response_model=BandejaSiniestrosRespuesta,
    responses={422: _error("SOLICITUD_INVALIDA (también si el cursor no es válido)")},
)
async def bandeja(
    actor: ActorDep,
    casos: ServiciosDep,
    bandeja: Annotated[TipoBandeja, Query(description="MIA: asignados al actor")] = TipoBandeja.MIA,
    estado: EstadoSiniestro | None = None,
    origen: OrigenSiniestro | None = None,
    prioridad: PrioridadSiniestro | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> BandejaSiniestrosRespuesta:
    """Bandeja del analista (H01) o cola general (H02). `q` busca por código de siniestro o por
    número de póliza (CF-S3-06)."""
    await _exigir_analista(casos, actor, RecursoSiniestro.BANDEJA)
    pagina = await casos.bandeja.ejecutar(
        FiltroBandeja(
            bandeja=bandeja,
            actor_id=_uuid_del_actor(actor),
            corte=casos.reloj.ahora(),
            plazos=casos.plazos,
            estado=estado,
            origen=origen,
            prioridad=prioridad,
            texto=q,
            limite=limite,
            cursor=cursor,
        )
    )
    _cierre("bandeja consultada", "siniestro.bandeja")
    return bandeja_dto(pagina)


@router.get(
    "/resumen",
    response_model=ResumenBandejaSiniestros,
    responses={422: _error("SOLICITUD_INVALIDA (también si desde ≥ hasta)")},
)
async def resumen(
    actor: ActorDep,
    casos: ServiciosDep,
    desde: Annotated[AwareDatetime, Query(description="Inicio (incluido), ISO-8601 con zona")],
    hasta: Annotated[AwareDatetime, Query(description="Fin (excluido), ISO-8601 con zona")],
) -> ResumenBandejaSiniestros:
    """Indicadores de la bandeja; `desde` y `hasta` delimitan el día del usuario."""
    await _exigir_analista(casos, actor, RecursoSiniestro.RESUMEN)
    indicadores = await casos.resumen.ejecutar(_uuid_del_actor(actor), desde, hasta)
    _cierre("resumen consultado", "siniestro.resumen")
    return indicadores_dto(indicadores)


@router.post(
    "/siguiente",
    response_model=ResumenSiniestro,
    responses={
        404: _error("COLA_SIN_CASOS: no hay siniestros REPORTADOS sin analista (estado H02)"),
        422: _error("SOLICITUD_INVALIDA o IDEMPOTENCIA_CONFLICTO"),
    },
)
async def tomar_siguiente(
    actor: ActorDep,
    casos: ServiciosDep,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ResumenSiniestro:
    """Asigna al actor el caso REPORTADO más antiguo (FOR UPDATE SKIP LOCKED) y lo pasa a
    EN_EVALUACION. La misma Idempotency-Key devuelve el mismo caso."""
    await _exigir_analista(casos, actor, RecursoSiniestro.SIGUIENTE)
    asignado = await casos.tomar.ejecutar(actor, _uuid_del_actor(actor), idempotency_key)
    _cierre("siniestro tomado", "siniestro.tomar", asignado.siniestro.id)
    return resumen_dto(asignado)


@router_accesos.post(
    "/accesos-denegados",
    status_code=202,
    response_class=Response,
    responses={422: _error("SOLICITUD_INVALIDA")},
)
async def registrar_acceso_denegado(
    actor: ActorDep, casos: ServiciosDep, solicitud: SolicitudAccesoDenegado
) -> Response:
    """Registra el rechazo de un usuario que `bff-web` no dejó pasar (DS3-09). Solo exige
    X-Actor-Id; no valida el rol (solo audita). Escribe AccesoSiniestroDenegado en el Outbox."""
    await casos.acceso.ejecutar(
        AccesoDenegado(
            actor_id=solicitud.actor_id,
            rol=actor.rol[:MAX_ROL] if actor.rol else None,
            recurso=solicitud.recurso,
            recurso_id=solicitud.recurso_id,
            motivo=solicitud.motivo,
            origen=OrigenDenegacion.BFF_WEB,
        )
    )
    _cierre("acceso denegado registrado", "siniestro.acceso_denegado")
    return Response(status_code=202)


@router.get(
    "/{siniestroId}",
    response_model=DetalleSiniestro,
    responses={404: _error("SINIESTRO_NO_ENCONTRADO")},
)
async def detalle(
    actor: ActorDep, casos: ServiciosDep, siniestro_id: IdSiniestro
) -> DetalleSiniestro:
    """Detalle del caso con la verificación de cobertura y la coherencia de la evidencia. Una
    evidencia cuyo objeto no existe viene como NO_DISPONIBLE y no impide la respuesta (AC4).
    Abrir un caso no lo asigna."""
    await _exigir_analista(casos, actor, RecursoSiniestro.SINIESTRO, siniestro_id)
    consultado = await casos.detalle.ejecutar(siniestro_id, actor)
    _cierre("siniestro consultado", "siniestro.consultar", siniestro_id)
    return detalle_dto(consultado)


@router.post(
    "/{siniestroId}/evidencias/{evidenciaId}/enlace",
    response_model=EnlaceEvidencia,
    responses={
        404: _error(
            "SINIESTRO_NO_ENCONTRADO, EVIDENCIA_NO_ENCONTRADA o EVIDENCIA_NO_DISPONIBLE (el "
            "objeto no existe: no se emite la URL)"
        ),
        503: _error("DEPENDENCIA_NO_DISPONIBLE (detalles.dependencia = evidencias)"),
    },
)
async def emitir_enlace(
    actor: ActorDep, casos: ServiciosDep, siniestro_id: IdSiniestro, evidencia_id: IdEvidencia
) -> EnlaceEvidencia:
    """Emite una URL prefirmada de lectura (≤ 300 s) y escribe EvidenciaConsultada en el Outbox.
    La URL no se guarda ni se registra."""
    await _exigir_analista(casos, actor, RecursoSiniestro.EVIDENCIA, siniestro_id)
    enlace = await casos.enlace.ejecutar(siniestro_id, evidencia_id, actor)
    _cierre("enlace de evidencia emitido", "evidencia.enlace", siniestro_id)
    return enlace_dto(enlace)
