"""Rutas del siniestro paramétrico de `siniestros` (HU-W22, W22-B01). Los decoradores, las firmas y
los docstrings son el contrato (`tests/contract/test_openapi.py`): vienen del prototipo de B1.

- `POST /v1/siniestros/parametricos` (DS3-23): lo llama `pagos-parametricos` con
  `X-Actor-Rol = SISTEMA_PARAMETRICO` y `Idempotency-Key = <eventoId>:<polizaId>`. 201 si crea el
  siniestro, 200 si ya existía (con el mismo `id`).
- `GET /v1/siniestros/parametricos/pagos?siniestroIds=` (R-181): lectura por lote del estado de pago
  que `bff-web` usa en H09. Rol `ANALISTA_SINIESTROS`, como el resto de las lecturas.

Orden de las rutas: `main.py` incluye este router **antes** que el de `/{siniestroId}`, para que
`parametricos` nunca se lea como un identificador de siniestro (como `catalogos-decision`, W20).

Log de cierre: solo identificadores y el resultado. **Nunca** el valor observado, el umbral, la
fuente, el cuerpo del evento, el cliente ni el `beneficiarioRef` (SEC-W22-01).
"""

import logging
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from solventa_common.errors import RutaB2

from siniestros.adapters.inbound.http.conversion import pagos_parametricos_dto, resumen_dto
from siniestros.adapters.inbound.http.dto import (
    PagosParametricosRespuesta,
    ResumenSiniestro,
    SolicitudSiniestroParametrico,
)
from siniestros.adapters.inbound.http.siniestros import (
    MAX_ROL,
    ActorDep,
    Servicios,
    ServiciosDep,
    _error,
    _exigir_analista,
)
from siniestros.application.siniestro.errores import RolNoAutorizado, SolicitudInvalida
from siniestros.application.siniestro.vistas import (
    AccesoDenegado,
    Actor,
    MotivoDenegacion,
    OrigenDenegacion,
    RecursoSiniestro,
)
from siniestros.domain.siniestro.errores import SiniestroInvalido
from siniestros.domain.siniestro.modelos import EventoParametrico, Vigencia
from siniestros.domain.siniestro.parametrico import ROL_SISTEMA, DatosParametrico, validar_valor

logger = logging.getLogger("siniestros.http")

router = APIRouter(
    prefix="/v1/siniestros/parametricos",
    tags=["parametricos"],
    route_class=RutaB2,
    responses={401: _error("ACTOR_REQUERIDO: falta X-Actor-Id")},
)


async def _exigir_sistema(servicios_: Servicios, actor: Actor) -> None:
    """Solo el sistema paramétrico crea estos siniestros; otro rol se audita y se rechaza (DS3-09)."""
    if actor.rol == ROL_SISTEMA:
        return
    await servicios_.acceso.ejecutar(
        AccesoDenegado(
            actor_id=actor.id,
            rol=actor.rol[:MAX_ROL] if actor.rol else None,
            recurso=RecursoSiniestro.SINIESTRO,
            recurso_id=None,
            motivo=MotivoDenegacion.ROL_AUSENTE if not actor.rol else MotivoDenegacion.ROL_INVALIDO,
            origen=OrigenDenegacion.SINIESTROS,
        )
    )
    raise RolNoAutorizado


def datos_de(solicitud: SolicitudSiniestroParametrico) -> DatosParametrico:
    """El cuerpo validado, como lo pide el caso de uso. Lo que el esquema no expresa (la moneda de la
    póliza, el valor positivo y las fechas de calendario) se comprueba aquí: 422 con el campo."""
    if solicitud.moneda != solicitud.poliza.moneda:
        raise SolicitudInvalida(["moneda"])
    try:
        valor = validar_valor(solicitud.valor)
    except SiniestroInvalido:
        raise SolicitudInvalida(["valor"]) from None
    try:
        vigencia = Vigencia(
            date.fromisoformat(solicitud.poliza.vigencia.desde),
            date.fromisoformat(solicitud.poliza.vigencia.hasta),
        )
    except ValueError:
        raise SolicitudInvalida(["poliza.vigencia"]) from None
    if vigencia.desde > vigencia.hasta:
        raise SolicitudInvalida(["poliza.vigencia"])
    e = solicitud.evento
    try:
        evento = EventoParametrico(
            evento_id=solicitud.evento_id,
            fuente=e.fuente,
            tipo_evento=e.tipo_evento,
            valor_observado=e.valor_observado,
            umbral=e.umbral,
            unidad=e.unidad,
            zona=e.zona,
            observado_en=e.observado_en,
            evento_origen=e.evento_origen,
            regla=solicitud.regla,
            version_regla=solicitud.version_regla,
        )
    except SiniestroInvalido:
        raise SolicitudInvalida(["evento.observadoEn"]) from None
    return DatosParametrico(
        evento=evento,
        poliza_id=solicitud.poliza_id,
        cliente_id=solicitud.cliente_id,
        numero=solicitud.poliza.numero,
        ramo=solicitud.poliza.ramo,
        vigencia=vigencia,
        moneda=solicitud.poliza.moneda,
        valor=valor,
    )


@router.post(
    "",
    status_code=201,
    response_model=ResumenSiniestro,
    responses={
        200: {
            "model": ResumenSiniestro,
            "description": "El siniestro de ese evento y esa póliza ya existía: es el mismo `id`, "
            "sin escribir nada nuevo",
        },
        403: _error(
            "ROL_NO_AUTORIZADO: X-Actor-Rol falta o no es SISTEMA_PARAMETRICO (se audita con "
            "AccesoSiniestroDenegado)"
        ),
        422: _error(
            "SOLICITUD_INVALIDA (detalles.campos; también si la Idempotency-Key no es "
            "<eventoId>:<polizaId> o la moneda no es la de la póliza) o IDEMPOTENCIA_CONFLICTO (la "
            "misma clave con otro cuerpo)"
        ),
    },
)
async def crear_parametrico(
    actor: ActorDep,
    casos: ServiciosDep,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            pattern=r"^[0-9a-fA-F-]{36}:[0-9a-fA-F-]{36}$",
            description="<eventoId>:<polizaId>: la clave de negocio del caso (UNIQUE evento-póliza)",
        ),
    ],
    solicitud: SolicitudSiniestroParametrico,
) -> ResumenSiniestro:
    """Crea el siniestro paramétrico de un evento que activó una póliza: nace APROBADO, sin
    analista, con la póliza, el evento, la regla y su versión y `valorIndemnizar` igual al valor de
    la cobertura (`deducible` 0). En una sola transacción escribe el siniestro, el historial
    `CREADO_AUTOMATICAMENTE` (actor `SISTEMA_PARAMETRICO`) y `SiniestroAprobado` en el Outbox. La
    clave `(eventoId, polizaId)` es única: repetir la solicitud, o recibirla dos réplicas a la vez,
    devuelve el siniestro existente (200)."""
    await _exigir_sistema(casos, actor)
    resultado = await casos.crear_parametrico.ejecutar(idempotency_key, datos_de(solicitud))
    if not resultado.creado:
        response.status_code = 200
    s = resultado.resumen.siniestro
    logger.info(
        "siniestro: parametrico creado" if resultado.creado else "siniestro: parametrico existente",
        extra={
            "siniestro_id": str(s.id),
            "evento_id": str(solicitud.evento_id),
            "poliza_id": str(solicitud.poliza_id),
            "operacion": "siniestro.crear_parametrico",
            "resultado": "creado" if resultado.creado else "existente",
        },
    )
    return resumen_dto(resultado.resumen)


@router.get(
    "/pagos",
    response_model=PagosParametricosRespuesta,
    responses={
        403: _error(
            "ROL_NO_AUTORIZADO: X-Actor-Rol falta o no es ANALISTA_SINIESTROS (se audita con "
            "AccesoSiniestroDenegado)"
        ),
        422: _error("SOLICITUD_INVALIDA (sin ids, más de 20 o alguno no es un UUID)"),
    },
)
async def pagos_parametricos(
    actor: ActorDep,
    casos: ServiciosDep,
    siniestro_ids: Annotated[
        list[UUID],
        Query(
            alias="siniestroIds",
            min_length=1,
            max_length=20,
            description="Hasta 20 ids; el parámetro se repite (siniestroIds=…&siniestroIds=…)",
        ),
    ],
) -> PagosParametricosRespuesta:
    """Estado de pago de los siniestros paramétricos pedidos (H09, «Últimos pagos activados»), en una
    sola consulta: el derivado de `PagoDto` (PENDIENTE hasta que `dinero` informa su resultado). Los
    ids inexistentes o de otro origen se omiten. No escribe nada ni audita una consulta."""
    await _exigir_analista(casos, actor, RecursoSiniestro.SINIESTRO)
    pagos = await casos.pagos_parametricos.ejecutar(siniestro_ids)
    return pagos_parametricos_dto(pagos)
