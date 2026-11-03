"""API interna de estado y listado de pólizas (HU-W18); la invoca `bff-web` para el asesor.

El actor viaja en `X-Actor-Id` y `X-Actor-Rol=ASESOR`; el rol se valida antes de tocar la base.
Todo asesor ve todas las pólizas proyectadas (como `puede_leer` de `cotizacion`); lo que no existe
o aún no se proyectó responde un único 404 uniforme. El detalle lleva el `clienteId` para que
`bff-web` componga el titular (no llega al navegador); el listado no lleva datos del cliente.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from consulta.application.estado.casos import (
    BuscarPolizas,
    ConsultarEstadoPoliza,
    EstadoPoliza,
    PaginaPolizas,
)
from consulta.domain.polizas.estado import EstadoVigencia

ROL_ASESOR = "ASESOR"
TAMANO_MAXIMO = 50

router = APIRouter(prefix="/v1/polizas", tags=["estado-polizas"])


def actor_asesor(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> str:
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol != ROL_ASESOR:
        raise ErrorNegocio(ROL_NO_AUTORIZADO, "No autorizado", 403)
    return x_actor_id


class Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


class VigenciaDto(Dto):
    desde: date
    hasta: date


class CoberturaDto(Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class EstadoPolizaDto(Dto):
    """`cliente_id` es para que `bff-web` componga el titular; no llega al navegador."""

    poliza_id: UUID
    cliente_id: UUID
    numero: str
    estado: EstadoVigencia
    fecha_actualizacion: datetime
    producto: str
    ramo: str
    vigencia: VigenciaDto
    prima: Decimal
    suma_asegurada: Decimal
    moneda: str
    periodicidad: str
    emitida_en: datetime
    coberturas: list[CoberturaDto]

    @classmethod
    def desde(cls, consultada: EstadoPoliza) -> "EstadoPolizaDto":
        p = consultada.poliza
        return cls(
            poliza_id=p.poliza_id,
            cliente_id=p.cliente_id,
            numero=p.numero,
            estado=consultada.estado,
            fecha_actualizacion=p.proyectada_en,
            producto=p.producto_nombre,
            ramo=p.ramo,
            vigencia=VigenciaDto(desde=p.vigencia.desde, hasta=p.vigencia.hasta),
            prima=p.prima,
            suma_asegurada=p.suma_asegurada,
            moneda=p.moneda,
            periodicidad=p.periodicidad,
            emitida_en=p.emitida_en,
            coberturas=[
                CoberturaDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
                for c in p.coberturas
            ],
        )


class ResumenPolizaDto(Dto):
    poliza_id: UUID
    numero: str
    ramo: str
    producto: str
    prima: Decimal
    moneda: str
    vigencia_hasta: date
    estado: EstadoVigencia
    fecha_actualizacion: datetime

    @classmethod
    def desde(cls, consultada: EstadoPoliza) -> "ResumenPolizaDto":
        p = consultada.poliza
        return cls(
            poliza_id=p.poliza_id,
            numero=p.numero,
            ramo=p.ramo,
            producto=p.producto_nombre,
            prima=p.prima,
            moneda=p.moneda,
            vigencia_hasta=p.vigencia.hasta,
            estado=consultada.estado,
            fecha_actualizacion=p.proyectada_en,
        )


class PaginaPolizasDto(Dto):
    items: list[ResumenPolizaDto]
    total: int
    pagina: int
    tamano: int
    actualizado_en: datetime | None

    @classmethod
    def desde(cls, pagina: PaginaPolizas) -> "PaginaPolizasDto":
        return cls(
            items=[ResumenPolizaDto.desde(i) for i in pagina.items],
            total=pagina.total,
            pagina=pagina.pagina,
            tamano=pagina.tamano,
            actualizado_en=pagina.actualizado_en,
        )


def _errores(*extra: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    base = [
        (401, "ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        (403, "ROL_NO_AUTORIZADO: X-Actor-Rol distinto de ASESOR"),
        (422, "SOLICITUD_INVALIDA: parámetro inválido (sin el valor recibido)"),
        *extra,
    ]
    return {estado: {"model": ErrorDto, "description": texto} for estado, texto in base}


@router.get(
    "",
    response_model=PaginaPolizasDto,
    response_model_by_alias=True,
    summary="Listado paginado de pólizas con su estado, por emitida_en descendente",
    responses=_errores(),
    dependencies=[Depends(actor_asesor)],
)
async def listar(
    request: Request,
    numero: Annotated[str | None, Query(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")] = None,
    ramo: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    estado: EstadoVigencia | None = None,
    vigencia_desde: Annotated[date | None, Query(alias="vigenciaDesde")] = None,
    pagina: Annotated[int, Query(ge=1, le=100_000)] = 1,
    tamano: Annotated[int, Query(ge=1, le=TAMANO_MAXIMO)] = 20,
) -> PaginaPolizasDto:
    caso: BuscarPolizas = request.app.state.buscar_polizas
    resultado = await caso.ejecutar(
        pagina=pagina,
        tamano=tamano,
        numero=numero,
        ramo=ramo,
        estado=estado,
        vigencia_desde=vigencia_desde,
    )
    return PaginaPolizasDto.desde(resultado)


@router.get(
    "/{poliza_id}/estado",
    response_model=EstadoPolizaDto,
    response_model_by_alias=True,
    summary="Estado vigente de una póliza y su fecha de actualización",
    responses=_errores((404, "POLIZA_NO_ENCONTRADA: no existe o aún no se proyectó")),
    dependencies=[Depends(actor_asesor)],
)
async def estado_de_poliza(poliza_id: UUID, request: Request) -> EstadoPolizaDto:
    caso: ConsultarEstadoPoliza = request.app.state.consultar_estado_poliza
    consultada = await caso.ejecutar(poliza_id)
    if consultada is None:
        raise ErrorNegocio("POLIZA_NO_ENCONTRADA", "La póliza no existe", 404)
    return EstadoPolizaDto.desde(consultada)
