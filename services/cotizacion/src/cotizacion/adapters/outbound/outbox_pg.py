"""Transactional Outbox: los eventos del perfil se escriben en la misma transacción (DT-15).

La publicación a SNS la hace el relay (fuera de este alcance); aquí solo se insertan filas.
"""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from cotizacion.adapters.outbound.eventos import (
    DATOS_CONSENTIDOS_CONSULTADOS,
    PERFIL_RIESGO_CALCULADO,
    DatoRequeridoEvento,
    DatosConsentidosConsultadosV1,
    FactorEvento,
    FuenteConsultadaEvento,
    PerfilRiesgoCalculadoV1,
    SobreEvento,
)
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    EstadoCampo,
    Perfil,
)

_INSERTAR = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def eventos_de(perfil: Perfil, correlation_id: str) -> list[SobreEvento[Any]]:
    """PerfilRiesgoCalculado y DatosConsentidosConsultados del perfil, sin valores financieros."""
    resultado = perfil.resultado
    calculado = PerfilRiesgoCalculadoV1(
        perfil_id=perfil.id,
        cliente_id=perfil.cliente_id,
        producto_id=perfil.producto_id,
        estado=resultado.estado,
        nivel_riesgo=resultado.nivel,
        version_modelo=perfil.version_modelo,
        hash_modelo=perfil.hash_modelo,
        factores=[
            FactorEvento(codigo=f.codigo, direccion=f.direccion, estado=f.estado)
            for f in resultado.factores
        ],
        datos_requeridos=[
            DatoRequeridoEvento(campo=d.campo, fuente=d.fuente, motivo=d.motivo)
            for d in resultado.datos_requeridos
        ],
        perfil_anterior_id=perfil.perfil_anterior_id,
        actor_id=perfil.actor_id,
        calculado_en=perfil.calculado_en,
    )
    consultados = DatosConsentidosConsultadosV1(
        perfil_id=perfil.id,
        cliente_id=perfil.cliente_id,
        consentimiento_id=perfil.consentimiento.consentimiento_id
        if perfil.consentimiento
        else None,
        proposito=PROPOSITO_PERFILAMIENTO,
        fuentes=[
            FuenteConsultadaEvento(
                fuente=r.fuente,
                estado=r.estado,
                campos_solicitados=list(r.campos_solicitados),
                campos_disponibles=[
                    c for c, v in r.campos.items() if v.estado is EstadoCampo.DISPONIBLE
                ],
                version_fuente=r.version_fuente,
                consultada_en=r.consultada_en,
            )
            for r in perfil.fuentes
        ],
    )
    return [
        SobreEvento[PerfilRiesgoCalculadoV1](
            event_id=uuid4(),
            aggregate_id=perfil.id,
            type=PERFIL_RIESGO_CALCULADO,
            version=1,
            occurred_at=perfil.calculado_en,
            correlation_id=correlation_id,
            payload=calculado,
        ),
        SobreEvento[DatosConsentidosConsultadosV1](
            event_id=uuid4(),
            aggregate_id=perfil.id,
            type=DATOS_CONSENTIDOS_CONSULTADOS,
            version=1,
            occurred_at=perfil.calculado_en,
            correlation_id=correlation_id,
            payload=consultados,
        ),
    ]


async def insertar(conexion: AsyncConnection[Any], sobres: Sequence[SobreEvento[Any]]) -> None:
    """Inserta los sobres en la transacción abierta de `conexion`."""
    for sobre in sobres:
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
