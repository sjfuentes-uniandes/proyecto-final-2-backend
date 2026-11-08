"""Transactional Outbox de siniestros (HU-W19): los eventos se escriben en la misma transacción que
el cambio o la consulta; el relay de `solventa_common.outbox` los publica a SNS. Aquí solo se
insertan filas (`published_at` solo lo marca el relay).

Ningún evento lleva la URL prefirmada, la clave del objeto ni nombres de personas.
"""

from datetime import datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from solventa_common import correlation

from siniestros.adapters.outbound.eventos import (
    ACCESO_SINIESTRO_DENEGADO,
    EVIDENCIA_CONSULTADA,
    SINIESTRO_CONSULTADO,
    SINIESTRO_EN_EVALUACION,
    AccesoSiniestroDenegadoV1,
    EvidenciaConsultadaV1,
    PayloadSiniestro,
    SiniestroConsultadoV1,
    SiniestroEnEvaluacionV1,
    SobreEvento,
)
from siniestros.application.siniestro.vistas import AccesoDenegado
from siniestros.domain.siniestro.modelos import Evidencia, Siniestro

_INSERTAR = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def _correlacion() -> str:
    return correlation.obtener() or str(uuid4())


def _sobre[P: PayloadSiniestro](
    tipo: str, aggregate_id: UUID, ocurrido_en: datetime, payload: P, event_id: UUID | None = None
) -> SobreEvento[P]:
    return SobreEvento[P](
        event_id=event_id or uuid4(),
        aggregate_id=aggregate_id,
        type=tipo,
        version=1,
        occurred_at=ocurrido_en,
        correlation_id=_correlacion(),
        payload=payload,
    )


def en_evaluacion(
    siniestro: Siniestro, ocurrido_en: datetime
) -> SobreEvento[SiniestroEnEvaluacionV1]:
    assert siniestro.analista_id is not None  # invariante de EN_EVALUACION
    return _sobre(
        SINIESTRO_EN_EVALUACION,
        siniestro.id,
        ocurrido_en,
        SiniestroEnEvaluacionV1(
            siniestro_id=siniestro.id,
            codigo=siniestro.codigo,
            poliza_id=siniestro.poliza_id,
            analista_id=siniestro.analista_id,
            motivo="ASIGNACION",
        ),
    )


def consultado(
    siniestro: Siniestro, actor_id: str, ocurrido_en: datetime
) -> SobreEvento[SiniestroConsultadoV1]:
    return _sobre(
        SINIESTRO_CONSULTADO,
        siniestro.id,
        ocurrido_en,
        SiniestroConsultadoV1(
            siniestro_id=siniestro.id, codigo=siniestro.codigo, actor_id=actor_id
        ),
    )


def evidencia_consultada(
    evidencia: Evidencia, actor_id: str, expira_en: datetime, ocurrido_en: datetime
) -> SobreEvento[EvidenciaConsultadaV1]:
    return _sobre(
        EVIDENCIA_CONSULTADA,
        evidencia.siniestro_id,
        ocurrido_en,
        EvidenciaConsultadaV1(
            siniestro_id=evidencia.siniestro_id,
            evidencia_id=evidencia.id,
            actor_id=actor_id,
            expira_en=expira_en,
        ),
    )


def acceso_denegado(
    acceso: AccesoDenegado, ocurrido_en: datetime
) -> SobreEvento[AccesoSiniestroDenegadoV1]:
    """`aggregateId` = `recursoId` si es un UUID; si no (la bandeja), el propio `eventId`."""
    event_id = uuid4()
    try:
        aggregate_id = UUID(acceso.recurso_id) if acceso.recurso_id else event_id
    except ValueError:
        aggregate_id = event_id
    return _sobre(
        ACCESO_SINIESTRO_DENEGADO,
        aggregate_id,
        ocurrido_en,
        AccesoSiniestroDenegadoV1(
            actor_id=acceso.actor_id,
            rol=acceso.rol,
            recurso=acceso.recurso.value,
            recurso_id=acceso.recurso_id,
            motivo=acceso.motivo.value,
            origen=acceso.origen.value,
        ),
        event_id=event_id,
    )


async def insertar(conexion: AsyncConnection, sobre: SobreEvento) -> None:
    await conexion.execute(
        _INSERTAR,
        (
            sobre.event_id,
            sobre.aggregate_id,
            sobre.type,
            sobre.version,
            sobre.occurred_at,
            sobre.correlation_id,
            Jsonb(sobre.payload.model_dump(mode="json", by_alias=True)),
        ),
    )
