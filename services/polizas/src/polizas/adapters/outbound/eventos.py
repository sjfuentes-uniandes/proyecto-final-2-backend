"""Sobre y payloads v1 de los eventos de polizas.

- DecisionSuscripcionRegistrada (HU-W13, W13-B03): auditoría de la decisión; sin montos ni datos
  personales: resultado, reglas, versión y huellas.
- OfertaAceptada (HU-W15, W15-B01): habilita el recaudo (HU-W16). Lleva la prima que se cobra,
  pero ningún dato personal ni de medios de pago; `claveRecaudo` (= `aceptacionId`) es la clave
  idempotente estable para `dinero`. `type` es el atributo `eventType` que filtra la cola
  `dinero-aceptaciones`.

Los esquemas publicados se regeneran con:
    uv run python -m polizas.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from polizas.domain.aceptacion.modelos import CanalAceptacion
from polizas.domain.suscripcion.modelos import ResultadoDecision, TipoDecision

DECISION_SUSCRIPCION_REGISTRADA = "DecisionSuscripcionRegistrada"
OFERTA_ACEPTADA = "OfertaAceptada"


class _Evento(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class DecisionSuscripcionRegistradaV1(_Evento):
    """Auditoría de la decisión: sin montos ni datos personales."""

    decision_id: UUID
    cotizacion_id: UUID
    oferta_id: UUID | None
    cliente_id: UUID
    producto_id: str
    resultado: ResultadoDecision
    tipo: TipoDecision
    regla_determinante: str | None
    reglas_no_cumplidas: list[str]
    version_reglas: str
    hash_reglas: str
    hash_insumos: str
    actor_id: str
    decidida_en: datetime


class PrimaAceptadaV1(_Evento):
    """La prima de la oferta aceptada; el valor es texto decimal (DS2-16)."""

    valor: Annotated[str, Field(pattern=r"^\d+(\.\d+)?$")]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: str


class OfertaAceptadaV1(_Evento):
    """Aceptación registrada: habilita el recaudo. Sin datos personales ni de medios de pago."""

    aceptacion_id: UUID
    cotizacion_id: UUID
    # Null si la decisión es anterior a HU-W04 (sin oferta propia).
    oferta_id: UUID | None
    decision_id: UUID
    cliente_id: UUID
    producto_id: str
    prima: PrimaAceptadaV1
    # Clave idempotente del recaudo (= aceptacionId): una aceptación, un cobro.
    clave_recaudo: UUID
    huella_calculo: str
    vigente_hasta: datetime
    version_declaraciones: str
    aceptada_en: datetime
    actor_id: str
    canal: CanalAceptacion


class SobreEvento[P: DecisionSuscripcionRegistradaV1 | OfertaAceptadaV1](_Evento):
    """Sobre versionado: el consumidor elige el esquema del payload por `type` y `version`."""

    event_id: UUID
    aggregate_id: UUID
    type: str
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: P


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{DECISION_SUSCRIPCION_REGISTRADA}.v1.schema.json": SobreEvento[
        DecisionSuscripcionRegistradaV1
    ],
    f"{OFERTA_ACEPTADA}.v1.schema.json": SobreEvento[OfertaAceptadaV1],
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
