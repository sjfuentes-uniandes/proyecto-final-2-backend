"""Transactional Outbox de polizas (regla 3): el evento se escribe en la misma transacción que la
decisión o la aceptación. La publicación a SNS la hace el relay (EXT-04); aquí solo se insertan filas.

HU-W17 (17.8): `PolizaEmitida` v1, en la transacción que deja la póliza ACTIVA (la de la tarea
emisora), con la referencia registrada del documento y de la firma.
"""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from polizas.adapters.outbound.eventos import (
    DECISION_SUSCRIPCION_REGISTRADA,
    OFERTA_ACEPTADA,
    POLIZA_EMITIDA,
    CoberturaV1,
    DecisionSuscripcionRegistradaV1,
    DocumentoV1,
    FirmaV1,
    OfertaAceptadaV1,
    PolizaEmitidaV1,
    PrimaAceptadaV1,
    ProductoV1,
    RamoV1,
    SobreEvento,
    SobrePolizaEmitidaV1,
    VigenciaV1,
)
from polizas.adapters.outbound.polizas_pg import TransaccionPg
from polizas.domain.aceptacion.modelos import Aceptacion
from polizas.domain.poliza.documento import DatosDocumento
from polizas.domain.poliza.modelos import EstadoPoliza, Poliza
from polizas.domain.suscripcion.insumos import texto
from polizas.domain.suscripcion.modelos import Decision

_INSERTAR = (
    "INSERT INTO outbox (event_id, aggregate_id, event_type, event_version, occurred_at, "
    "correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def evento_de(decision: Decision) -> SobreEvento[DecisionSuscripcionRegistradaV1]:
    """DecisionSuscripcionRegistrada.v1 de la decisión, sin montos ni insumos."""
    evaluacion = decision.evaluacion
    return SobreEvento[DecisionSuscripcionRegistradaV1](
        event_id=uuid4(),
        aggregate_id=decision.id,
        type=DECISION_SUSCRIPCION_REGISTRADA,
        version=1,
        occurred_at=decision.decidida_en,
        correlation_id=decision.correlation_id,
        payload=DecisionSuscripcionRegistradaV1(
            decision_id=decision.id,
            cotizacion_id=decision.cotizacion_id,
            oferta_id=decision.oferta_id,
            cliente_id=decision.cliente_id,
            producto_id=decision.producto_id,
            resultado=decision.resultado,
            tipo=decision.tipo,
            regla_determinante=evaluacion.regla_determinante,
            reglas_no_cumplidas=list(evaluacion.reglas_no_cumplidas),
            version_reglas=decision.version_reglas,
            hash_reglas=decision.hash_reglas,
            hash_insumos=decision.hash_insumos,
            actor_id=decision.actor_id,
            decidida_en=decision.decidida_en,
        ),
    )


def evento_de_aceptacion(
    aceptacion: Aceptacion, decision: Decision
) -> SobreEvento[OfertaAceptadaV1]:
    """OfertaAceptada.v1: la prima sale de la instantánea de la decisión (DS2-10)."""
    insumos = decision.insumos
    return SobreEvento[OfertaAceptadaV1](
        event_id=uuid4(),
        aggregate_id=aceptacion.id,
        type=OFERTA_ACEPTADA,
        version=1,
        occurred_at=aceptacion.aceptada_en,
        correlation_id=aceptacion.correlation_id,
        payload=OfertaAceptadaV1(
            aceptacion_id=aceptacion.id,
            cotizacion_id=aceptacion.cotizacion_id,
            oferta_id=aceptacion.oferta_id,
            decision_id=aceptacion.decision_id,
            cliente_id=aceptacion.cliente_id,
            producto_id=decision.producto_id,
            prima=PrimaAceptadaV1(
                valor=texto(insumos.prima_total),
                moneda=insumos.moneda,
                periodicidad=insumos.periodicidad,
            ),
            clave_recaudo=aceptacion.id,
            huella_calculo=aceptacion.huella_calculo,
            vigente_hasta=aceptacion.vigente_hasta,
            version_declaraciones=aceptacion.version_declaraciones,
            aceptada_en=aceptacion.aceptada_en,
            actor_id=aceptacion.actor_id,
            canal=aceptacion.canal,
        ),
    )


def evento_de_emision(
    poliza: Poliza, datos: DatosDocumento, correlation_id: str
) -> SobrePolizaEmitidaV1:
    """PolizaEmitida.v1 de una póliza ACTIVA: `occurredAt` = `emitidaEn`; la prima, la suma, la
    moneda, la periodicidad y las coberturas, de los datos de su documento (la instantánea de la
    decisión, nunca el catálogo); el ramo, de la póliza."""
    documento, firma, emitida_en = poliza.documento, poliza.firma, poliza.emitida_en
    # Una póliza ACTIVA siempre tiene documento, firma y emitida_en (Poliza y el CHECK de 004).
    activa = poliza.estado is EstadoPoliza.ACTIVA
    if not activa or documento is None or firma is None or emitida_en is None:
        raise ValueError("PolizaEmitida: solo de una póliza ACTIVA")
    return SobrePolizaEmitidaV1(
        event_id=uuid4(),
        aggregate_id=poliza.poliza_id,
        type=POLIZA_EMITIDA,
        version=1,
        occurred_at=emitida_en,
        correlation_id=correlation_id,
        payload=PolizaEmitidaV1(
            poliza_id=poliza.poliza_id,
            numero=poliza.numero,
            cotizacion_id=poliza.cotizacion_id,
            clave_recaudo=poliza.aceptacion_id,
            cliente_id=poliza.cliente_id,
            producto=ProductoV1(id=poliza.producto.id, version=poliza.producto.version),
            ramo=RamoV1(codigo=poliza.ramo.codigo, nombre=poliza.ramo.nombre),
            coberturas=[
                CoberturaV1(codigo=c.codigo, nombre=c.nombre, obligatoria=c.obligatoria)
                for c in datos.coberturas
            ],
            vigencia=VigenciaV1(desde=poliza.vigencia.desde, hasta=poliza.vigencia.hasta),
            prima=texto(datos.prima),
            suma_asegurada=texto(datos.suma_asegurada),
            moneda=datos.moneda,
            periodicidad=datos.periodicidad,
            documento=DocumentoV1(
                clave=documento.clave,
                version_id=documento.version_id,
                huella=documento.huella,
                tamano_bytes=documento.tamano_bytes,
            ),
            firma=FirmaV1(firma_id=firma.firma_id, sello_tiempo=firma.sello_tiempo),
            emitida_en=emitida_en,
        ),
    )


class OutboxPolizasPg:
    """Implementa EventosPoliza[TransaccionPg] (HU-W17, 17.8)."""

    async def emitida(
        self, poliza: Poliza, datos: DatosDocumento, correlation_id: str, tx: TransaccionPg
    ) -> None:
        await insertar(tx.conexion, [evento_de_emision(poliza, datos, correlation_id)])


async def insertar(conexion: AsyncConnection[Any], sobres: Sequence[SobreEvento[Any]]) -> None:
    """Inserta los sobres en la transacción abierta de `conexion` (el payload en su columna)."""
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
