"""Solo para bff-web: con encabezados X-Partner-* (un socio) responde 404."""

import asyncio
import logging
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from solventa_common.errors import ErrorNegocio

from api_socios.adapters.inbound.dependencias import administrar_socios
from api_socios.adapters.inbound.errores import (
    SOLICITUD_INVALIDA,
    error,
    no_encontrado,
    plan_no_disponible,
    socio_desactivado,
    socio_no_encontrado,
)
from api_socios.application.socios import AdministrarSocios
from api_socios.domain.cuotas import Consumo, EstadoCuota, LimitesPlan, Periodo, PlanNoDisponible
from api_socios.domain.socios import (
    CanalVenta,
    Estado,
    NuevoSocio,
    Permiso,
    Plan,
    Socio,
    SocioDesactivado,
    SocioNoEncontrado,
)

logger = logging.getLogger("api_socios.admin")


def solo_administracion(
    x_partner_client_id: Annotated[str | None, Header(include_in_schema=False)] = None,
    x_partner_key_id: Annotated[str | None, Header(include_in_schema=False)] = None,
    x_authenticated_sub: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> str | None:
    if x_partner_client_id is not None or x_partner_key_id is not None:
        logger.warning(
            "socio intentó usar la administración", extra={"client_id": x_partner_client_id}
        )
        raise no_encontrado()
    return x_authenticated_sub


router = APIRouter(
    prefix="/admin/socios",
    tags=["administracion"],
    dependencies=[Depends(solo_administracion)],
    responses={
        400: error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        404: error(
            "SOCIO_NO_ENCONTRADO, o NO_ENCONTRADO si la solicitud llega desde el API de socios"
        ),
        422: error("SOLICITUD_INVALIDA (detalles.campos)"),
    },
)

Administrador = Annotated[str | None, Depends(solo_administracion)]
Caso = Annotated[AdministrarSocios, Depends(administrar_socios)]
IdSocio = Annotated[str, Path(pattern=r"^SOC-\d{6}$")]
_CONTROL = r"\x00-\x1f\x7f-\x9f"
# Igual que NuevoSocio: sin caracteres de control y dos visibles después de quitar espacios.
RAZON_SOCIAL = rf"^[^\S{_CONTROL}]*[^\s{_CONTROL}][^{_CONTROL}]*[^\s{_CONTROL}][^\S{_CONTROL}]*$"


class SocioSolicitud(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(
        min_length=2, max_length=120, pattern=RAZON_SOCIAL, description="Razón social"
    )
    canal: CanalVenta
    plan: Plan
    permisos: list[Permiso] = Field(min_length=1, max_length=len(Permiso))


class SocioRespuesta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    nombre: str
    canal: CanalVenta
    plan: Plan
    permisos: list[Permiso]
    estado: Estado
    client_id: str = Field(alias="clientId")
    creado: datetime
    desactivado: datetime | None


class SocioCreado(SocioRespuesta):
    """Única respuesta que contiene el secreto y la API key (AC1)."""

    client_secret: str = Field(alias="clientSecret")
    api_key: str = Field(alias="apiKey")


class CambioPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: Plan


class LimitesRespuesta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    plan: Plan
    limite_por_segundo: float = Field(alias="limitePorSegundo")
    rafaga: int
    cuota: int
    periodo: Periodo


class VentanaRespuesta(BaseModel):
    inicio: date
    fin: date
    reinicio: datetime


class ConsumoRespuesta(BaseModel):
    """Solo datos del socio consultado: nunca su API key ni los de otro socio."""

    model_config = ConfigDict(populate_by_name=True)

    socio_id: str = Field(alias="socioId")
    limites: LimitesRespuesta
    ventana: VentanaRespuesta
    usado: int
    restante: int
    porcentaje: int
    estado: EstadoCuota
    limitadas: int | None = Field(
        description="Respuestas 429 en la ventana; null si no hay métricas"
    )


def limites_respuesta(limites: LimitesPlan) -> LimitesRespuesta:
    return LimitesRespuesta(
        plan=limites.plan,
        limite_por_segundo=limites.limite_por_segundo,
        rafaga=limites.rafaga,
        cuota=limites.cuota,
        periodo=limites.periodo,
    )


def consumo_respuesta(socio_id: str, consumo: Consumo) -> ConsumoRespuesta:
    return ConsumoRespuesta(
        socio_id=socio_id,
        limites=limites_respuesta(consumo.limites),
        ventana=VentanaRespuesta(
            inicio=consumo.ventana.inicio,
            fin=consumo.ventana.fin,
            reinicio=consumo.ventana.reinicio,
        ),
        usado=consumo.usado,
        restante=consumo.restante,
        porcentaje=consumo.porcentaje,
        estado=consumo.estado,
        limitadas=consumo.limitadas,
    )


def respuesta(socio: Socio) -> SocioRespuesta:
    return SocioRespuesta(
        id=socio.id,
        nombre=socio.nombre,
        canal=socio.canal,
        plan=socio.plan,
        permisos=sorted(socio.permisos),
        estado=socio.estado,
        client_id=socio.client_id,
        creado=socio.creado,
        desactivado=socio.desactivado,
    )


def _buscar(caso: AdministrarSocios, socio_id: str) -> Socio:
    try:
        return caso.obtener(socio_id)
    except SocioNoEncontrado as error:
        raise socio_no_encontrado() from error


@router.get("", response_model=list[SocioRespuesta], response_model_by_alias=True)
async def listar_socios(caso: Caso) -> list[SocioRespuesta]:
    return [respuesta(s) for s in await asyncio.to_thread(caso.listar)]


@router.post(
    "",
    response_model=SocioCreado,
    response_model_by_alias=True,
    status_code=201,
    responses={422: error("SOLICITUD_INVALIDA o PLAN_NO_DISPONIBLE (detalles.plan)")},
)
async def habilitar_socio(
    solicitud: SocioSolicitud, caso: Caso, administrador: Administrador, http: Response
) -> SocioCreado:
    try:
        nuevo = NuevoSocio(
            nombre=solicitud.nombre,
            canal=solicitud.canal,
            plan=solicitud.plan,
            permisos=frozenset(solicitud.permisos),
        )
    except ValueError as error:
        campo = "permisos" if "permiso" in str(error) else "nombre"
        raise ErrorNegocio(SOLICITUD_INVALIDA, str(error), 422, {"campos": [campo]}) from error
    try:
        socio, credenciales = await asyncio.to_thread(caso.habilitar, nuevo)
    except PlanNoDisponible as falla:
        raise plan_no_disponible(solicitud.plan.value) from falla
    logger.info(
        "alta de socio solicitada", extra={"socio_id": socio.id, "administrador": administrador}
    )
    # El secreto no debe quedar en cachés.
    http.headers["Cache-Control"] = "no-store"
    return SocioCreado(
        **respuesta(socio).model_dump(),
        client_secret=credenciales.client_secret,
        api_key=credenciales.api_key,
    )


@router.get("/planes", response_model=list[LimitesRespuesta], response_model_by_alias=True)
async def listar_planes(caso: Caso) -> list[LimitesRespuesta]:
    return [limites_respuesta(p) for p in await asyncio.to_thread(caso.planes)]


@router.get("/{socio_id}", response_model=SocioRespuesta, response_model_by_alias=True)
async def consultar_socio(socio_id: IdSocio, caso: Caso) -> SocioRespuesta:
    return respuesta(await asyncio.to_thread(_buscar, caso, socio_id))


@router.post("/{socio_id}/desactivar", response_model=SocioRespuesta, response_model_by_alias=True)
async def desactivar_socio(
    socio_id: IdSocio, caso: Caso, administrador: Administrador
) -> SocioRespuesta:
    try:
        socio = await asyncio.to_thread(caso.desactivar, socio_id)
    except SocioNoEncontrado as error:
        raise socio_no_encontrado() from error
    logger.info(
        "desactivación de socio solicitada",
        extra={"socio_id": socio_id, "administrador": administrador},
    )
    return respuesta(socio)


@router.put(
    "/{socio_id}/plan",
    response_model=SocioRespuesta,
    response_model_by_alias=True,
    responses={
        409: error("SOCIO_DESACTIVADO"),
        422: error("SOLICITUD_INVALIDA o PLAN_NO_DISPONIBLE (detalles.plan)"),
    },
)
async def cambiar_plan(
    socio_id: IdSocio, solicitud: CambioPlan, caso: Caso, administrador: Administrador
) -> SocioRespuesta:
    try:
        socio = await asyncio.to_thread(caso.cambiar_plan, socio_id, solicitud.plan)
    except SocioNoEncontrado as falla:
        raise socio_no_encontrado() from falla
    except SocioDesactivado as falla:
        raise socio_desactivado() from falla
    except PlanNoDisponible as falla:
        raise plan_no_disponible(solicitud.plan.value) from falla
    logger.info(
        "cambio de plan solicitado",
        extra={"socio_id": socio_id, "plan": solicitud.plan.value, "administrador": administrador},
    )
    return respuesta(socio)


@router.get("/{socio_id}/consumo", response_model=ConsumoRespuesta, response_model_by_alias=True)
async def consultar_consumo(socio_id: IdSocio, caso: Caso) -> ConsumoRespuesta:
    try:
        consumo = await asyncio.to_thread(caso.consumo, socio_id)
    except SocioNoEncontrado as falla:
        raise socio_no_encontrado() from falla
    return consumo_respuesta(socio_id, consumo)
