"""Rutas de H09 de `bff-web` (HU-W22, W22-B03 y B10), con las mismas dependencias, el mismo grupo
`analistas-siniestros` y la misma falla cerrada que las rutas de `siniestros.py` (vienen del
prototipo de B1). El cuerpo se valida contra el DTO antes de devolverlo: si no lo cumple, 503.

- `GET /v1/siniestros/parametricos/resumen?eventoOrigen=`: `ResumenParametricosVista`.
- `GET /v1/siniestros/parametricos/eventos-origen?limite=`: los orígenes recientes del selector.

Orden de las rutas: `main.py` incluye este router **antes** que el de `/v1/siniestros/{siniestroId}`
(y junto al de `parametricos/descartes`, W24), para que `parametricos` nunca se lea como un
identificador de siniestro.

Operación medida: `parametrico.resumen` (`bff-web`).
"""

from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import TypeAdapter, ValidationError
from solventa_common.errors import RutaB2

from bff_web.adapters.inbound.parametricos_dto import (
    OrigenesRecientesVista,
    ResumenParametricosVista,
)
from bff_web.adapters.inbound.siniestros import Actor, _error, exigir_grupo_analistas
from bff_web.adapters.outbound import cloudwatch
from bff_web.adapters.outbound.pagos_parametricos import PagosParametricosCliente
from bff_web.adapters.outbound.siniestros import SiniestrosCliente
from bff_web.application.descartes import servicio_no_disponible
from bff_web.application.parametricos import Parametricos
from bff_web.config import settings

router = APIRouter(
    prefix="/v1/siniestros/parametricos",
    tags=["siniestros"],
    route_class=RutaB2,
    dependencies=[Depends(exigir_grupo_analistas)],
    responses={
        401: _error("ACTOR_REQUERIDO: la solicitud no trae el usuario autenticado"),
        403: _error(
            "ROL_NO_AUTORIZADO: falta el grupo analistas-siniestros o no es del back-office "
            "(se notifica a siniestros, DS3-09)"
        ),
        422: _error("SOLICITUD_INVALIDA (detalles.campos)"),
        503: _error(
            "SERVICIO_NO_DISPONIBLE: pagos-parametricos no respondió o respondió fuera de su contrato"
        ),
    },
)


@lru_cache
def _metricas() -> cloudwatch.MetricasParametricos:
    return cloudwatch.crear_metricas_parametricos(settings.aws_region)


@lru_cache
def parametricos_servicio() -> Parametricos:
    return Parametricos(
        PagosParametricosCliente(
            settings.pagos_parametricos_url, settings.pagos_parametricos_timeout_ms
        ),
        SiniestrosCliente(settings.siniestros_url, settings.siniestros_timeout_ms),
        _metricas,
        settings.parametricos_cola_eventos
        or f"solventa-{settings.environment}-parametricos-eventos",
        settings.parametricos_cluster,
        settings.parametricos_servicio,
        settings.parametricos_consumidores_maximos,
        settings.parametricos_secundario_timeout_ms,
    )


Servicio = Annotated[Parametricos, Depends(parametricos_servicio)]
_RESUMEN = TypeAdapter(ResumenParametricosVista)
_ORIGENES = TypeAdapter(OrigenesRecientesVista)


def _validar(adaptador: TypeAdapter[Any], cuerpo: Any) -> Any:
    try:
        adaptador.validate_python(cuerpo)
    except ValidationError as error:
        raise servicio_no_disponible() from error
    return cuerpo


@router.get("/resumen", response_model=ResumenParametricosVista)
async def resumen_parametricos(
    servicio: Servicio,
    analista: Actor,
    evento_origen: Annotated[
        str | None,
        Query(
            alias="eventoOrigen",
            pattern=r"^[A-Za-z0-9._:-]{1,64}$",
            description="Sin valor, el origen de la evaluación más reciente",
        ),
    ] = None,
) -> ResumenParametricosVista:
    """H09: el resumen de `pagos-parametricos` más la cola y los consumidores (CloudWatch) y el
    estado de pago de cada fila (`siniestros`). Una fuente secundaria que falla deja su campo en
    null; solo la falta de `pagos-parametricos` es un 503."""
    return _validar(_RESUMEN, await servicio.resumen(evento_origen, analista))


@router.get("/eventos-origen", response_model=OrigenesRecientesVista)
async def eventos_origen(
    servicio: Servicio,
    analista: Actor,
    limite: Annotated[int, Query(ge=1, le=50)] = 10,
) -> OrigenesRecientesVista:
    """Los `eventoOrigen` recientes de `pagos-parametricos`, para el selector de H09."""
    return _validar(_ORIGENES, await servicio.origenes(limite, analista))
