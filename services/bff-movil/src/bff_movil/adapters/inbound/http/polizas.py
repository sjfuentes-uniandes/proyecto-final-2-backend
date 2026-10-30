"""API de pólizas del cliente autenticado: lista y detalle con su credencial (HU-M10).

El titular es siempre el del token: ninguna ruta, query ni cuerpo acepta un `clienteId`.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from solventa_common.errors import ErrorNegocio

from bff_movil.adapters.inbound.http.auth import IdentidadAutenticada, autenticar
from bff_movil.adapters.inbound.http.codigos import (
    CLIENTE_NO_ENCONTRADO,
    POLIZA_NO_AUTORIZADA,
    POLIZA_NO_ENCONTRADA,
    SERVICIO_NO_DISPONIBLE,
)
from bff_movil.adapters.inbound.http.comun import CABECERAS, Dto, error
from bff_movil.application.polizas.casos import ConsultarPoliza, ListarPolizas
from bff_movil.domain.polizas.modelos import Poliza, PolizaNoAutorizada, PolizaNoEncontrada
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible

router = APIRouter(prefix="/v1/polizas", tags=["polizas"])


class VigenciaDto(Dto):
    desde: date
    hasta: date


class ResumenCoberturaDto(Dto):
    suma_asegurada: Decimal
    moneda: str
    total_coberturas: int


class PolizaResumenDto(Dto):
    poliza_id: UUID
    numero: str
    estado: str
    producto: str
    ramo: str
    vigencia: VigenciaDto
    vigente: bool
    resumen_cobertura: ResumenCoberturaDto

    @classmethod
    def desde(cls, p: Poliza) -> "PolizaResumenDto":
        return cls(
            poliza_id=p.poliza_id,
            numero=p.numero,
            estado=p.estado,
            producto=p.producto_nombre,
            ramo=p.ramo,
            vigencia=VigenciaDto(desde=p.vigencia.desde, hasta=p.vigencia.hasta),
            vigente=p.vigente,
            resumen_cobertura=ResumenCoberturaDto(
                suma_asegurada=p.suma_asegurada,
                moneda=p.moneda,
                total_coberturas=len(p.coberturas),
            ),
        )


class CoberturaDto(Dto):
    codigo: str
    nombre: str
    obligatoria: bool


class CredencialDto(Dto):
    numero: str
    huella: str
    firma_id: str
    vigencia: VigenciaDto


class PolizaDetalleDto(Dto):
    numero: str
    estado: str
    producto: str
    ramo: str
    vigencia: VigenciaDto
    vigente: bool
    prima: Decimal
    suma_asegurada: Decimal
    moneda: str
    periodicidad: str
    emitida_en: datetime
    coberturas: list[CoberturaDto]
    credencial: CredencialDto

    @classmethod
    def desde(cls, p: Poliza) -> "PolizaDetalleDto":
        vigencia = VigenciaDto(desde=p.vigencia.desde, hasta=p.vigencia.hasta)
        return cls(
            numero=p.numero,
            estado=p.estado,
            producto=p.producto_nombre,
            ramo=p.ramo,
            vigencia=vigencia,
            vigente=p.vigente,
            prima=p.prima,
            suma_asegurada=p.suma_asegurada,
            moneda=p.moneda,
            periodicidad=p.periodicidad,
            emitida_en=p.emitida_en,
            coberturas=[
                CoberturaDto(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
                for c in p.coberturas
            ],
            credencial=CredencialDto(
                numero=p.numero,
                huella=p.documento_huella,
                firma_id=p.firma_id,
                vigencia=vigencia,
            ),
        )


_NO_AUTENTICADO = error(
    "NO_AUTENTICADO: falta el token, venció, tiene una firma inválida o no corresponde "
    "al titular autenticado. La app debe pedir autenticación completa"
)
_ROL = error("ROL_NO_AUTORIZADO: el token no es del pool de clientes")
_NO_CLIENTE = error("CLIENTE_NO_ENCONTRADO")
_NO_DISPONIBLE = error("SERVICIO_NO_DISPONIBLE: no se pudo consultar las pólizas")


def _no_disponible() -> ErrorNegocio:
    return ErrorNegocio(SERVICIO_NO_DISPONIBLE, "Servicio no disponible", 503)


@router.get(
    "",
    response_model=list[PolizaResumenDto],
    response_model_by_alias=True,
    summary="Pólizas del cliente autenticado, incluidas las vencidas",
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: _ROL,
        404: _NO_CLIENTE,
        422: error("SOLICITUD_INVALIDA: un encabezado no cumple su formato"),
        503: _NO_DISPONIBLE,
    },
)
async def listar(
    request: Request, identidad: Annotated[IdentidadAutenticada, Depends(autenticar)]
) -> list[PolizaResumenDto]:
    caso: ListarPolizas = request.app.state.listar_polizas
    try:
        polizas = await caso.ejecutar(identidad.sub)
    except ClienteNoEncontrado as e:
        raise ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404) from e
    except ServicioNoDisponible as e:
        raise _no_disponible() from e
    return [PolizaResumenDto.desde(p) for p in polizas]


@router.get(
    "/{poliza_id}",
    response_model=PolizaDetalleDto,
    response_model_by_alias=True,
    summary="Detalle de una póliza del cliente autenticado con su credencial",
    responses={
        200: {"headers": CABECERAS},
        401: _NO_AUTENTICADO,
        403: error(
            "ROL_NO_AUTORIZADO: el token no es del pool de clientes, o "
            "POLIZA_NO_AUTORIZADA: la póliza es de otro cliente"
        ),
        404: error("CLIENTE_NO_ENCONTRADO o POLIZA_NO_ENCONTRADA"),
        422: error("SOLICITUD_INVALIDA: el identificador o un encabezado no cumplen su formato"),
        503: _NO_DISPONIBLE,
    },
)
async def detalle(
    poliza_id: UUID,
    request: Request,
    identidad: Annotated[IdentidadAutenticada, Depends(autenticar)],
) -> PolizaDetalleDto:
    caso: ConsultarPoliza = request.app.state.consultar_poliza
    try:
        poliza = await caso.ejecutar(identidad.sub, poliza_id)
    except ClienteNoEncontrado as e:
        raise ErrorNegocio(CLIENTE_NO_ENCONTRADO, "El cliente no existe", 404) from e
    except PolizaNoEncontrada as e:
        raise ErrorNegocio(POLIZA_NO_ENCONTRADA, "La póliza no existe", 404) from e
    except PolizaNoAutorizada as e:
        raise ErrorNegocio(POLIZA_NO_AUTORIZADA, "La póliza no es del cliente", 403) from e
    except ServicioNoDisponible as e:
        raise _no_disponible() from e
    return PolizaDetalleDto.desde(poliza)
