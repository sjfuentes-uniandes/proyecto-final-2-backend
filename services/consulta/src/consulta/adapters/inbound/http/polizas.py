"""API interna de lectura de pólizas del cliente (HU-M10); la invoca `bff-movil`.

Mismo mecanismo que `clientes`: el `clienteId` viaja en la ruta (lo resuelve el BFF desde el `sub`,
nunca el móvil) y el actor en `X-Actor-Id` y `X-Actor-Rol=CLIENTE`. Una póliza de otro cliente
responde 403 sin ningún dato de ella.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from solventa_common.errors import ROL_NO_AUTORIZADO, ErrorNegocio

from consulta.application.lectura.casos import ConsultarPoliza, ListarPolizas, PolizaConsultada
from consulta.application.lectura.puertos import PolizaNoAutorizada, PolizaNoEncontrada

ROL_CLIENTE = "CLIENTE"

router = APIRouter(prefix="/v1/clientes/{cliente_id}/polizas", tags=["polizas"])


def actor_cliente(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> str:
    if not x_actor_id or not x_actor_rol:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Faltan los encabezados del actor", 401)
    if x_actor_rol != ROL_CLIENTE:
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


class PolizaDto(Dto):
    poliza_id: UUID
    numero: str
    estado: str
    producto_id: str
    producto_version: str
    producto_nombre: str
    ramo: str
    vigencia: VigenciaDto
    vigente: bool
    prima: Decimal
    suma_asegurada: Decimal
    moneda: str
    periodicidad: str
    coberturas: list[CoberturaDto]
    documento_huella: str
    firma_id: str
    emitida_en: datetime

    @classmethod
    def desde(cls, consultada: PolizaConsultada) -> "PolizaDto":
        p = consultada.poliza
        return cls(
            poliza_id=p.poliza_id,
            numero=p.numero,
            estado=p.estado,
            producto_id=p.producto_id,
            producto_version=p.producto_version,
            producto_nombre=p.producto_nombre,
            ramo=p.ramo,
            vigencia=VigenciaDto(desde=p.vigencia.desde, hasta=p.vigencia.hasta),
            vigente=consultada.vigente,
            prima=p.prima,
            suma_asegurada=p.suma_asegurada,
            moneda=p.moneda,
            periodicidad=p.periodicidad,
            coberturas=[
                CoberturaDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
                for c in p.coberturas
            ],
            documento_huella=p.documento_huella,
            firma_id=p.firma_id,
            emitida_en=p.emitida_en,
        )


def _errores(*extra: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    base = [
        (401, "ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        (403, "ROL_NO_AUTORIZADO: X-Actor-Rol distinto de CLIENTE"),
        *extra,
    ]
    return {estado: {"model": ErrorDto, "description": texto} for estado, texto in base}


@router.get(
    "",
    response_model=list[PolizaDto],
    response_model_by_alias=True,
    summary="Pólizas del cliente, incluidas las vencidas, por vigencia_hasta descendente",
    responses=_errores(),
    dependencies=[Depends(actor_cliente)],
)
async def listar(cliente_id: UUID, request: Request) -> list[PolizaDto]:
    caso: ListarPolizas = request.app.state.listar_polizas
    return [PolizaDto.desde(p) for p in await caso.ejecutar(cliente_id)]


@router.get(
    "/{poliza_id}",
    response_model=PolizaDto,
    response_model_by_alias=True,
    summary="Detalle de una póliza del cliente",
    responses=_errores(
        (403, "POLIZA_NO_AUTORIZADA: la póliza es de otro cliente (sin datos de la póliza)"),
        (404, "POLIZA_NO_ENCONTRADA"),
    ),
    dependencies=[Depends(actor_cliente)],
)
async def detalle(cliente_id: UUID, poliza_id: UUID, request: Request) -> PolizaDto:
    caso: ConsultarPoliza = request.app.state.consultar_poliza
    try:
        return PolizaDto.desde(await caso.ejecutar(cliente_id, poliza_id))
    except PolizaNoEncontrada as error:
        raise ErrorNegocio("POLIZA_NO_ENCONTRADA", "La póliza no existe", 404) from error
    except PolizaNoAutorizada as error:
        raise ErrorNegocio("POLIZA_NO_AUTORIZADA", "La póliza no es del cliente", 403) from error
