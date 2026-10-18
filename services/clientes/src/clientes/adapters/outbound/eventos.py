"""Sobres y payloads v1 de `ConsentimientoOtorgado` y `ConsentimientoRevocado` (ADR 0009) y de
`VerificacionConsentimientoRegistrada` (HU-W31).

Solo identificadores y códigos: sin datos personales. Los esquemas publicados se regeneran con:
    uv run python -m clientes.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from clientes.domain.consentimientos.verificacion import CausaRechazo, Decision

CONSENTIMIENTO_OTORGADO = "ConsentimientoOtorgado"
CONSENTIMIENTO_REVOCADO = "ConsentimientoRevocado"
VERIFICACION_REGISTRADA = "VerificacionConsentimientoRegistrada"


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


class ConsentimientoRevocadoV1(_Dto):
    consentimiento_id: UUID
    cliente_id: UUID
    revocado_en: datetime
    actor_id: str


class SobreConsentimientoRevocado(_Dto):
    """Sobre versionado. `aggregateId` es el `consentimientoId`."""

    event_id: UUID
    aggregate_id: UUID
    type: Literal["ConsentimientoRevocado"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    # Opcional y aditivo, como en Otorgado: el relay lo deriva del `type` (ADR 0009).
    aggregate_version: Literal[2] = Field(
        default=2, description="Posición en la cadena del consentimiento: Revocado es 2"
    )
    payload: ConsentimientoRevocadoV1


class VerificacionConsentimientoRegistradaV1(_Dto):
    """La decisión de una verificación (W31 AC1 y AC3). `consentimientoId` es el consentimiento
    evaluado, también si estaba revocado o vencido; nulo si no hay ninguno del propósito."""

    cliente_id: UUID
    consentimiento_id: UUID | None
    proposito: str
    alcances_solicitados: list[str] = Field(min_length=1)
    decision: Decision
    causa: CausaRechazo | None
    alcances_rechazados: list[str]
    verificado_en: datetime


class SobreVerificacionConsentimientoRegistrada(_Dto):
    """Sobre versionado. `aggregateId` es siempre el `clienteId` y no lleva `aggregateVersion`:
    las verificaciones no pertenecen a la cadena de un consentimiento (ADR 0009)."""

    event_id: UUID
    aggregate_id: UUID
    type: Literal["VerificacionConsentimientoRegistrada"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: VerificacionConsentimientoRegistradaV1


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{CONSENTIMIENTO_OTORGADO}.v1.schema.json": SobreConsentimientoOtorgado,
    f"{CONSENTIMIENTO_REVOCADO}.v1.schema.json": SobreConsentimientoRevocado,
}
# Aparte de los del agregado: van solo a `auditoria`, sin `aggregateVersion` (ADR 0009).
ESQUEMAS_VERIFICACION: dict[str, type[BaseModel]] = {
    f"{VERIFICACION_REGISTRADA}.v1.schema.json": SobreVerificacionConsentimientoRegistrada,
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in (ESQUEMAS | ESQUEMAS_VERIFICACION).items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
