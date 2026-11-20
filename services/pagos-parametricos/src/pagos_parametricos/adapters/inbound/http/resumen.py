"""API de seguimiento de `pagos-parametricos` (HU-W22, W22-B02 y B09). Los DTO y los decoradores son el
contrato: `tests/contract/test_openapi.py` compara el YAML con `app.openapi()`. Vienen del prototipo
de B1 (`docs/sprint_3/evidencias/hu-w22/prototipos/pagos_parametricos_resumen.py`).

- `GET /v1/parametricos/resumen?eventoOrigen=`: lo que H09 muestra de un episodio (indicadores,
  ritmo de los últimos 15 minutos y últimos pagos activados). Sin `eventoOrigen`, el más reciente.
- `GET /v1/parametricos/eventos-origen?limite=`: los orígenes recientes, para el selector de H09.

Actor por encabezados, como la cola de descarte (`descartes.analista`): `X-Actor-Id` y
`X-Actor-Rol = ANALISTA_SINIESTROS`; sin ellos 401, con otro rol 403. Solo lectura.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from solventa_common.errors import ErrorNegocio

from pagos_parametricos.adapters.inbound.http.descartes import _error, analista
from pagos_parametricos.application.parametrico import resumen as casos

TextoDecimal = Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,3})?$")]
EventoOrigen = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
CodigoSiniestro = Annotated[str, Field(pattern=r"^88-[0-9]{4}-[0-9]{4,}$")]
NumeroPoliza = Annotated[str, Field(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")]
MINUTOS_DE_RITMO = casos.MINUTOS_DE_RITMO
DEPENDENCIA_NO_DISPONIBLE = "DEPENDENCIA_NO_DISPONIBLE"


class EstadoActivacion(StrEnum):
    """`PENDIENTE` es el estado inicial de W21 y no sale en la lista: solo se listan las activaciones
    con siniestro. W22 define `SINIESTRO_CREADO`."""

    SINIESTRO_CREADO = "SINIESTRO_CREADO"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RitmoMinuto(_Dto):
    minuto: datetime = Field(description="Inicio del minuto (UTC)")
    procesados: int = Field(description="Eventos evaluados en ese minuto")


class Disparador(_Dto):
    """Lo que activó el pago: la medición contra el umbral contratado."""

    tipo_evento: str
    valor_observado: TextoDecimal
    umbral: TextoDecimal
    unidad: str


class PagoActivado(_Dto):
    siniestro_id: UUID
    siniestro_codigo: CodigoSiniestro
    poliza_id: UUID
    poliza_numero: NumeroPoliza | None = Field(
        description="Null si la cobertura no conserva el número de la póliza"
    )
    disparador: Disparador
    valor: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,2})?$")]
    moneda: str = Field(pattern=r"^[A-Z]{3}$")
    estado_activacion: EstadoActivacion


class ResumenParametricos(_Dto):
    """Seguimiento del procesamiento de un `eventoOrigen` (H09). Sin eventos todavía, `eventoOrigen`
    y `desde` son null y todo lo demás va en cero o vacío."""

    evento_origen: EventoOrigen | None
    desde: datetime | None = Field(description="Primera evaluación del origen (UTC)")
    recibidos: int = Field(
        description="Entregas que llegaron al evaluador: `procesados` más `duplicados`"
    )
    procesados: int = Field(description="Eventos distintos ya evaluados, cada uno una sola vez")
    activados: int = Field(
        description="Activaciones (una por evento y póliza que superó el umbral), con o sin "
        "siniestro todavía"
    )
    duplicados: int = Field(
        description="Entregas repetidas que se descartaron sin cambiar la decisión ni crear otro "
        "siniestro (clave de negocio y reentregas reanudadas, DS3-15)"
    )
    en_dlq: int | None = Field(
        description="Mensajes en la DLQ de `parametricos-eventos` (toda la cola, no solo el origen); "
        "null si no se puede leer"
    )
    ritmo_por_minuto: list[RitmoMinuto] = Field(
        description="Los últimos 15 minutos, del más antiguo al más reciente, completos (con ceros)"
    )
    ultimos_pagos: list[PagoActivado] = Field(
        description="Hasta 10 activaciones con siniestro, la más reciente primero"
    )


class OrigenReciente(_Dto):
    evento_origen: EventoOrigen
    desde: datetime = Field(description="Primera evaluación del origen (UTC)")
    hasta: datetime = Field(description="Última evaluación del origen (UTC)")
    eventos: int = Field(description="Eventos distintos evaluados")


class OrigenesRecientes(_Dto):
    items: list[OrigenReciente] = Field(description="El de la evaluación más reciente primero")


def _servicio(request: Request) -> casos.ConsultarResumen:
    servicio: casos.ConsultarResumen | None = getattr(request.app.state, "resumen", None)
    if servicio is None:
        raise ErrorNegocio(DEPENDENCIA_NO_DISPONIBLE, "La base de datos no está configurada", 503)
    return servicio


Servicio = Annotated[casos.ConsultarResumen, Depends(_servicio)]


def pago_dto(p: casos.PagoActivado) -> PagoActivado:
    return PagoActivado(
        siniestro_id=p.siniestro_id,
        siniestro_codigo=p.siniestro_codigo,
        poliza_id=p.poliza_id,
        poliza_numero=p.poliza_numero,
        disparador=Disparador(
            tipo_evento=p.disparador.tipo_evento,
            valor_observado=p.disparador.valor_observado,
            umbral=p.disparador.umbral,
            unidad=p.disparador.unidad,
        ),
        valor=p.valor,
        moneda=p.moneda,
        estado_activacion=EstadoActivacion(p.estado_activacion),
    )


def resumen_dto(r: casos.Resumen) -> ResumenParametricos:
    return ResumenParametricos(
        evento_origen=r.evento_origen,
        desde=r.desde,
        recibidos=r.recibidos,
        procesados=r.procesados,
        activados=r.activados,
        duplicados=r.duplicados,
        en_dlq=r.en_dlq,
        ritmo_por_minuto=[
            RitmoMinuto(minuto=m.minuto, procesados=m.procesados) for m in r.ritmo_por_minuto
        ],
        ultimos_pagos=[pago_dto(p) for p in r.ultimos_pagos],
    )


router = APIRouter(
    prefix="/v1/parametricos",
    tags=["resumen"],
    dependencies=[Depends(analista)],
    responses={
        401: _error("ACTOR_REQUERIDO: faltan X-Actor-Id o X-Actor-Rol"),
        403: _error("ROL_NO_AUTORIZADO: X-Actor-Rol no es ANALISTA_SINIESTROS"),
        503: _error("DEPENDENCIA_NO_DISPONIBLE: la base no está configurada o no responde"),
    },
)


@router.get(
    "/resumen",
    response_model=ResumenParametricos,
    responses={422: _error("SOLICITUD_INVALIDA (detalles.campos)")},
)
async def resumen(
    servicio: Servicio,
    evento_origen: Annotated[
        str | None,
        Query(
            alias="eventoOrigen",
            pattern=r"^[A-Za-z0-9._:-]{1,64}$",
            description="Sin valor, el origen de la evaluación más reciente",
        ),
    ] = None,
) -> ResumenParametricos:
    """Indicadores, ritmo y últimos pagos de un episodio paramétrico (H09). Lectura con los índices
    por `evento_origen` y `evaluado_en`; una consulta no recorre el resto de la tabla (DS3-19)."""
    return resumen_dto(await servicio.resumen(evento_origen))


@router.get(
    "/eventos-origen",
    response_model=OrigenesRecientes,
    responses={422: _error("SOLICITUD_INVALIDA (detalles.campos)")},
)
async def eventos_origen(
    servicio: Servicio,
    limite: Annotated[int, Query(ge=1, le=50)] = 10,
) -> OrigenesRecientes:
    """Los `eventoOrigen` recientes, para el selector de H09 cuando hay varios episodios."""
    origenes = await servicio.origenes(limite)
    return OrigenesRecientes(
        items=[
            OrigenReciente(
                evento_origen=o.evento_origen, desde=o.desde, hasta=o.hasta, eventos=o.eventos
            )
            for o in origenes
        ]
    )
