"""API POST /v1/consultas (03 §B.4.1)."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio
from solventa_common.resiliencia import CORTE_DURO_MS, DependenciaNoDisponible, TiempoAgotado

from adaptador_datos.application.consultas.consultar_fuente import ConsultarFuente
from adaptador_datos.domain.consultas.modelos import (
    CamposNoSoportados,
    ConsentimientoRequerido,
    EstadoValor,
    Fuente,
    RespuestaConsulta,
    RespuestaInvalida,
    TipoFuente,
)

router = APIRouter(prefix="/v1/consultas", tags=["consultas"])

# Tipo int con valor por defecto None: el contrato publica un entero opcional y no anulable
# (con `int | None`, Schemathesis envía "null", que el esquema permitiría).
DeadlineMs = Annotated[int, Header(alias="X-Deadline-Ms", ge=1, le=CORTE_DURO_MS)]


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _sin_duplicados(campos: list[str]) -> list[str]:
    if len(set(campos)) != len(campos):
        raise ValueError("campos duplicados")
    return campos


class SolicitudConsulta(_Dto):
    fuente: Fuente
    cliente_id: UUID
    consentimiento_id: UUID | None = None
    proposito: Literal["PERFILAMIENTO_PRECIO"]
    campos_autorizados: Annotated[
        list[Annotated[str, Field(min_length=1)]],
        Field(min_length=1),
        AfterValidator(_sin_duplicados),
    ]


class ValorCampoDto(_Dto):
    estado: EstadoValor
    valor: str | None = Field(description="Decimal en texto; null si no está disponible")


class RespuestaConsultaDto(_Dto):
    fuente: Fuente
    tipo: TipoFuente
    proveedor: str
    version_fuente: str
    confianza: float
    consultada_en: datetime
    campos: dict[str, ValorCampoDto]
    campos_descartados: int

    @classmethod
    def desde(cls, respuesta: RespuestaConsulta) -> "RespuestaConsultaDto":
        return cls(
            fuente=respuesta.fuente,
            tipo=respuesta.tipo,
            proveedor=respuesta.proveedor,
            version_fuente=respuesta.version_fuente,
            confianza=float(respuesta.confianza),
            consultada_en=respuesta.consultada_en,
            campos={
                campo: ValorCampoDto(
                    estado=valor.estado,
                    valor=None if valor.valor is None else str(valor.valor),
                )
                for campo, valor in respuesta.campos.items()
            },
            campos_descartados=respuesta.campos_descartados,
        )


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


def _caso_de_uso(request: Request) -> ConsultarFuente:
    caso: ConsultarFuente = request.app.state.consultar_fuente
    return caso


@router.post(
    "",
    response_model=RespuestaConsultaDto,
    response_model_by_alias=True,
    responses={
        400: _error("ERROR_HTTP: el cuerpo no se puede leer como JSON"),
        422: _error(
            "SOLICITUD_INVALIDA, CAMPOS_NO_SOPORTADOS o CONSENTIMIENTO_REQUERIDO "
            "(sin consentimiento no se llama al proveedor)"
        ),
        502: _error("RESPUESTA_INVALIDA"),
        503: _error("FUENTE_NO_DISPONIBLE (detalles.motivo y detalles.circuito)"),
        504: _error("TIEMPO_AGOTADO"),
    },
)
async def consultar(
    solicitud: SolicitudConsulta,
    request: Request,
    x_deadline_ms: DeadlineMs = None,  # type: ignore[assignment]
) -> RespuestaConsultaDto:
    """Consulta una fuente autorizada y devuelve solo los campos autorizados."""
    try:
        respuesta = await _caso_de_uso(request).ejecutar(
            fuente=solicitud.fuente,
            cliente_id=solicitud.cliente_id,
            consentimiento_id=solicitud.consentimiento_id,
            campos=solicitud.campos_autorizados,
            deadline_ms=x_deadline_ms,
        )
    except ConsentimientoRequerido as error:
        raise ErrorNegocio(
            "CONSENTIMIENTO_REQUERIDO", "La consulta requiere un consentimiento vigente", 422
        ) from error
    except CamposNoSoportados as error:
        raise ErrorNegocio(
            "CAMPOS_NO_SOPORTADOS",
            "Hay campos que la fuente no soporta",
            422,
            {"campos": list(error.campos_invalidos)},
        ) from error
    except TiempoAgotado as error:
        raise ErrorNegocio("TIEMPO_AGOTADO", "La fuente no respondió a tiempo", 504) from error
    except DependenciaNoDisponible as error:
        raise ErrorNegocio(
            "FUENTE_NO_DISPONIBLE",
            "La fuente no está disponible",
            503,
            {"motivo": error.motivo, "circuito": error.circuito},
        ) from error
    except RespuestaInvalida as error:
        raise ErrorNegocio(
            "RESPUESTA_INVALIDA", "La fuente respondió con un formato inválido", 502
        ) from error
    return RespuestaConsultaDto.desde(respuesta)
