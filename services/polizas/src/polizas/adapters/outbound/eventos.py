"""Sobre y payload v1 del evento de auditoría de la suscripción (HU-W13, W13-B03).

El payload no lleva montos ni datos personales: resultado, reglas, versión y huellas. El esquema
publicado se regenera con:
    uv run python -m polizas.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from polizas.domain.suscripcion.modelos import ResultadoDecision, TipoDecision

DECISION_SUSCRIPCION_REGISTRADA = "DecisionSuscripcionRegistrada"


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


class SobreEvento[P: DecisionSuscripcionRegistradaV1](_Evento):
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
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
