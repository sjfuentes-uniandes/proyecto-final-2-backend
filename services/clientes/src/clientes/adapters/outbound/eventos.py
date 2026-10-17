"""Sobre y payload v1 del evento `ConsentimientoOtorgado` (ADR 0009).

Solo identificadores y códigos: sin datos personales. El esquema publicado se regenera con:
    uv run python -m clientes.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

CONSENTIMIENTO_OTORGADO = "ConsentimientoOtorgado"


class _Dto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class ConsentimientoOtorgadoV1(_Dto):
    consentimiento_id: UUID
    cliente_id: UUID
    proposito: str
    alcances: list[str] = Field(min_length=1)
    version_texto: str
    otorgado_en: datetime
    vigente_hasta: datetime
    actor_id: str


class SobreConsentimientoOtorgado(_Dto):
    """Sobre versionado. `aggregateId` es el `consentimientoId`."""

    event_id: UUID
    aggregate_id: UUID
    type: Literal["ConsentimientoOtorgado"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    # Opcional y aditivo: el outbox no guarda la columna; el relay lo deriva del `type` (ADR 0009).
    aggregate_version: Literal[1] = Field(
        default=1, description="Posición en la cadena del consentimiento: Otorgado es 1"
    )
    payload: ConsentimientoOtorgadoV1


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{CONSENTIMIENTO_OTORGADO}.v1.schema.json": SobreConsentimientoOtorgado,
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
