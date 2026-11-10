"""Ruta interna `GET /v1/indemnizaciones?siniestroId=` de `dinero` (HU-W23, W23-B13).

Es la primera ruta HTTP de `dinero`. La llama el BFF con el actor del analista (`X-Actor-Id` y
`X-Actor-Rol = ANALISTA_SINIESTROS`); en la sincronización final (§2.7, SYNC-01) `bff-web` la usa
para componer H08. `dinero` figura sin *listener* en `infra/platform/catalog.tf` (R-104): hoy solo
es alcanzable en local y en las pruebas.

Responde en formato B2 (`RutaB2`). Sin base configurada la app arranca (para el healthcheck) pero no
atiende esta ruta: 503 `DEPENDENCIA_NO_DISPONIBLE`. Sin datos de la cuenta del beneficiario: ni el
`beneficiarioRef`.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio, RutaB2

from dinero.application.indemnizacion.consultar import ConsultarIndemnizaciones
from dinero.domain.indemnizacion.modelos import EstadoIndemnizacion as EstadoDominio
from dinero.domain.indemnizacion.modelos import Indemnizacion

logger = logging.getLogger("dinero.http")

ROL_ANALISTA = "ANALISTA_SINIESTROS"
MAX_ACTOR = 128
REFERENCIA_PAGO = r"^IND-88-[0-9]{4}-[0-9]{4,}-[0-9]{2}$"


class EstadoIndemnizacion(StrEnum):
    """`PENDIENTE` mientras no haya una respuesta definitiva de la pasarela (un timeout o un 5xx
    nunca es un rechazo, ADR 0013 decisión 5). De `CONFIRMADA` y `RECHAZADA` no se sale."""

    PENDIENTE = "PENDIENTE"
    CONFIRMADA = "CONFIRMADA"
    RECHAZADA = "RECHAZADA"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDto(BaseModel):
    codigo: str
    mensaje: str
    correlationId: str | None
    detalles: dict[str, Any]


class IndemnizacionDto(_Dto):
    id: UUID
    siniestro_id: UUID
    tipo: Literal["INDEMNIZACION"]
    referencia: str = Field(
        pattern=REFERENCIA_PAGO,
        description="IND-<codigo>-01: la clave única e idempotente hacia la pasarela (DS3-11)",
    )
    monto: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,2})?$", examples=["2500000.00"])]
    moneda: str = Field(pattern=r"^[A-Z]{3}$")
    estado: EstadoIndemnizacion
    transaccion_id: str | None = Field(
        description="Solo en CONFIRMADA: la confirmación de la pasarela"
    )
    motivo: str | None = Field(
        description="Solo en RECHAZADA: el código normalizado de la pasarela"
    )
    intentos: int = Field(
        ge=0, description="Llamadas efectivas de pago hechas a la pasarela por este movimiento"
    )
    duplicados_descartados: int = Field(
        ge=0,
        description="Veces que llegó la misma orden (evento repetido, o confirmación repetida de "
        "la pasarela) y se descartó sin crear otro movimiento",
    )
    creada_en: datetime
    confirmada_en: datetime | None = Field(description="Solo en CONFIRMADA")
    ultimo_intento_en: datetime | None = Field(
        description="Instante del último intento incierto (timeout, 5xx o respuesta inválida) que "
        "dejó el movimiento PENDIENTE; null si nunca lo hubo"
    )

    @classmethod
    def de(cls, i: Indemnizacion) -> "IndemnizacionDto":
        confirmada = i.estado is EstadoDominio.CONFIRMADA
        pendiente = i.estado is EstadoDominio.PENDIENTE
        return cls(
            id=i.indemnizacion_id,
            siniestro_id=i.siniestro_id,
            tipo="INDEMNIZACION",
            referencia=i.referencia,
            # Texto decimal exacto, con los decimales del dominio (a lo sumo 2).
            monto=format(i.monto, "f"),
            moneda=i.moneda.value,
            estado=EstadoIndemnizacion(i.estado.value),
            transaccion_id=i.transaccion_id,
            motivo=i.motivo,
            intentos=i.intentos,
            duplicados_descartados=i.duplicados_descartados,
            creada_en=i.creado_en,
            confirmada_en=i.resuelto_en if confirmada else None,
            # Solo mientras siga PENDIENTE: `actualizado_en` es el último intento incierto.
            ultimo_intento_en=i.actualizado_en if pendiente and i.ultimo_incierto else None,
        )


class ListaIndemnizacionesDto(_Dto):
    items: list[IndemnizacionDto] = Field(description="Vacía si el siniestro aún no tiene pago")


def _error(descripcion: str) -> dict[str, Any]:
    return {"model": ErrorDto, "description": descripcion}


@dataclass(frozen=True)
class Actor:
    id: str
    rol: str | None


def actor_dinero(
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_rol: Annotated[str | None, Header(alias="X-Actor-Rol")] = None,
) -> Actor:
    if not x_actor_id or not x_actor_id.strip() or len(x_actor_id) > MAX_ACTOR:
        raise ErrorNegocio("ACTOR_REQUERIDO", "Falta el actor de la solicitud", 401)
    return Actor(x_actor_id.strip(), x_actor_rol)


def consultar_indemnizaciones(request: Request) -> ConsultarIndemnizaciones:
    caso: ConsultarIndemnizaciones | None = getattr(request.app.state, "consultar", None)
    if caso is None:
        # Sin base configurada la app arranca (para el healthcheck) pero no atiende solicitudes.
        raise ErrorNegocio(
            "DEPENDENCIA_NO_DISPONIBLE",
            "La base de datos no está disponible",
            503,
            {"dependencia": "base de datos"},
        )
    return caso


router = APIRouter(
    prefix="/v1/indemnizaciones",
    tags=["indemnizaciones"],
    route_class=RutaB2,
    responses={
        401: _error("ACTOR_REQUERIDO: falta X-Actor-Id"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol falta o no es ANALISTA_SINIESTROS"),
        422: _error("SOLICITUD_INVALIDA: siniestroId falta o no es un UUID"),
        503: _error("DEPENDENCIA_NO_DISPONIBLE: la base de datos no está disponible"),
    },
)


@router.get("", response_model=ListaIndemnizacionesDto, response_model_by_alias=True)
async def listar(
    siniestro_id: Annotated[UUID, Query(alias="siniestroId")],
    actor: Annotated[Actor, Depends(actor_dinero)],
    caso: Annotated[ConsultarIndemnizaciones, Depends(consultar_indemnizaciones)],
) -> ListaIndemnizacionesDto:
    """Indemnizaciones de un siniestro (H08): estado, intentos y pagos duplicados descartados."""
    if actor.rol != ROL_ANALISTA:
        raise ErrorNegocio("ROL_NO_AUTORIZADO", "El rol no permite consultar indemnizaciones", 403)
    encontradas = await caso.ejecutar(siniestro_id)
    logger.info(
        "indemnizacion consultada",
        extra={"siniestro_id": str(siniestro_id), "operacion": "indemnizacion.consultar"},
    )
    return ListaIndemnizacionesDto(items=[IndemnizacionDto.de(i) for i in encontradas])
