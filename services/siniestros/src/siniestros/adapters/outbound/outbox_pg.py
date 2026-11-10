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
    SINIESTRO_APROBADO,
    SINIESTRO_CONSULTADO,
    SINIESTRO_EN_EVALUACION,
    SINIESTRO_RECHAZADO,
    SINIESTRO_SOPORTE_SOLICITADO,
    AccesoSiniestroDenegadoV1,
    EvidenciaConsultadaV1,
    PayloadSiniestro,
    SiniestroAprobadoV1,
    SiniestroConsultadoV1,
    SiniestroEnEvaluacionV1,
    SiniestroRechazadoV1,
    SiniestroSoporteSolicitadoV1,
    SobreEvento,
)
from siniestros.application.siniestro.vistas import AccesoDenegado
from siniestros.domain.siniestro.modelos import (
    CanalSoporte,
    CausalRechazo,
    Evidencia,
    Siniestro,
    SoporteSolicitable,
)

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
    siniestro: Siniestro, ocurrido_en: datetime, motivo: str = "ASIGNACION"
) -> SobreEvento[SiniestroEnEvaluacionV1]:
    """`motivo`: `ASIGNACION` (lo tomó un analista) o `PLAZO_SOPORTE_VENCIDO` (volvió a su bandeja)."""
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
            motivo=motivo,
        ),
    )


def aprobado(
    siniestro: Siniestro, referencia_pago: str, decidido_por: UUID, aprobado_en: datetime
) -> SobreEvento[SiniestroAprobadoV1]:
    """DS3-24. Solo el identificador opaco del beneficiario: nunca la cuenta."""
    beneficiario = siniestro.copia_poliza.beneficiario
    assert beneficiario is not None and siniestro.valor_indemnizar is not None
    return _sobre(
        SINIESTRO_APROBADO,
        siniestro.id,
        aprobado_en,
        SiniestroAprobadoV1(
            siniestro_id=siniestro.id,
            codigo=siniestro.codigo,
            origen=siniestro.origen.value,
            poliza_id=siniestro.poliza_id,
            cliente_id=siniestro.cliente_id,
            valor_indemnizar=f"{siniestro.valor_indemnizar:f}",
            moneda=siniestro.moneda or siniestro.copia_poliza.moneda,
            referencia_pago=referencia_pago,
            beneficiario_ref=beneficiario.beneficiario_ref,
            aprobado_en=aprobado_en,
            decidido_por=decidido_por,
        ),
    )


def rechazado(
    siniestro: Siniestro, causal: CausalRechazo, decidido_por: UUID, decidido_en: datetime
) -> SobreEvento[SiniestroRechazadoV1]:
    """Sin la explicación al cliente (texto libre)."""
    return _sobre(
        SINIESTRO_RECHAZADO,
        siniestro.id,
        decidido_en,
        SiniestroRechazadoV1(
            siniestro_id=siniestro.id,
            codigo=siniestro.codigo,
            poliza_id=siniestro.poliza_id,
            causal=causal.value,
            decidido_por=decidido_por,
            decidido_en=decidido_en,
        ),
    )


def soporte_solicitado(
    siniestro: Siniestro,
    documentos: tuple[SoporteSolicitable, ...],
    canal: CanalSoporte,
    decidido_por: UUID,
    ocurrido_en: datetime,
) -> SobreEvento[SiniestroSoporteSolicitadoV1]:
    """Sin el mensaje al cliente (texto libre)."""
    assert siniestro.plazo_soporte_vence_en is not None  # lo fija solicitar_soporte
    return _sobre(
        SINIESTRO_SOPORTE_SOLICITADO,
        siniestro.id,
        ocurrido_en,
        SiniestroSoporteSolicitadoV1(
            siniestro_id=siniestro.id,
            codigo=siniestro.codigo,
            poliza_id=siniestro.poliza_id,
            documentos=[d.value for d in documentos],
            canal=canal.value,
            plazo_vence_en=siniestro.plazo_soporte_vence_en,
            decidido_por=decidido_por,
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
