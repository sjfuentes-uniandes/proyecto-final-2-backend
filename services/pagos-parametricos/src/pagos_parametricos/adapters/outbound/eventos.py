"""Sobre y payload v1 de los eventos de `pagos-parametricos` que define HU-W24.

- DescarteParametricoResuelto (HU-W24, decisión B2): un analista resolvió un mensaje de la cola de
  descarte de `parametricos-eventos`: lo reprocesó tal cual, lo corrigió y reprocesó, o lo descartó.
  Va solo a `auditoria` (cola sin filtro; `modelo_informacion.puml`: `CanalEventos -->
  HistorialAuditoria`), que guarda el sobre entero: el **original** y, si se corrigió, la
  **corrección**, como pide H11. `aggregateId` = el `eventId` del evento resuelto (si el original
  no era un sobre, el `MessageId`, que SQS genera como uuid). `actorId` es el `sub` del analista.
  El contenido es el del evento de telemetría, sin datos personales.

- EventoParametricoEvaluado (HU-W21, W21-B02): el resultado de evaluar un evento observado contra
  las coberturas candidatas, una sola vez. Lo publica este servicio por su Outbox, en la misma
  transacción que escribe `eventos_evaluados`, `evaluaciones` y `activaciones`. `aggregateId` =
  `eventoId`; `eventId` = `uuid5(eventoId, "EventoParametricoEvaluado")`, así el Outbox no puede
  duplicarlo. `decision` es la del evento (`ACTIVA`, `NO_ACTIVA` o `SIN_POLIZAS`); la razón por
  póliza vive en `evaluaciones`. `fuente` e `idExterno` conservan el origen (SEC-W21-01). Sin
  pólizas, clientes ni valores: solo cuántas, la decisión y la versión de la regla.

Los esquemas publicados se regeneran con:
    uv run python -m pagos_parametricos.adapters.outbound.eventos
"""

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from pagos_parametricos.domain.descarte import Accion, Causa
from pagos_parametricos.domain.parametrico.modelos import DecisionEvento, Razon

DESCARTE_PARAMETRICO_RESUELTO = "DescarteParametricoResuelto"
EVENTO_PARAMETRICO_EVALUADO = "EventoParametricoEvaluado"

# Los mismos patrones que `EventoParametricoObservado`.
FUENTE = r"^[A-Z][A-Z0-9-]{1,31}$"
IDENTIFICADOR = r"^[A-Za-z0-9._:-]{1,64}$"


class _Evento(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class DescarteParametricoResueltoV1(_Evento):
    message_id: str = Field(description="El MessageId de SQS, el mismo en la cola y en la DLQ")
    evento_id: str | None = Field(description="El eventId del evento resuelto, si era un sobre")
    accion: Accion
    causa: Causa = Field(description="La causa del descarte que vio el analista")
    actor_id: str = Field(min_length=1, max_length=128)
    motivo: str | None = Field(default=None, max_length=500)
    original: str = Field(description="El cuerpo del mensaje tal como estaba en la DLQ")
    corregido: str | None = Field(description="El cuerpo reenviado, solo en CORREGIDO")
    resuelto_en: AwareDatetime


class SobreDescarteParametricoResueltoV1(_Evento):
    """Sobre versionado con `type` fijo: es el atributo `eventType` del mensaje."""

    event_id: UUID
    aggregate_id: UUID
    type: Literal["DescarteParametricoResuelto"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: DescarteParametricoResueltoV1


class RazonEvento(StrEnum):
    """Razón a nivel de evento; las de cada póliza están en `evaluaciones` (DS3-21)."""

    SIN_POLIZAS_EN_ZONA = Razon.SIN_POLIZAS_EN_ZONA.value


class EventoParametricoEvaluadoV1(_Evento):
    """El resultado de evaluar un evento observado contra las coberturas candidatas."""

    evento_id: UUID
    fuente: Annotated[str, Field(pattern=FUENTE)]
    id_externo: Annotated[str, Field(pattern=IDENTIFICADOR)]
    evento_origen: Annotated[str, Field(pattern=IDENTIFICADOR)]
    decision: DecisionEvento
    razon: RazonEvento | None = Field(
        description="SIN_POLIZAS_EN_ZONA cuando la decisión es SIN_POLIZAS; si no, null"
    )
    activaciones: Annotated[int, Field(ge=0, description="Pólizas que activaron la cobertura")]
    evaluadas: Annotated[int, Field(ge=0, description="Pólizas candidatas evaluadas")]
    version_regla: Annotated[str, Field(min_length=1, max_length=32)]
    evaluado_en: AwareDatetime


class SobreEventoParametricoEvaluadoV1(_Evento):
    """Sobre versionado con `type` fijo: es el atributo `eventType` del mensaje."""

    event_id: UUID
    aggregate_id: UUID
    type: Literal["EventoParametricoEvaluado"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: EventoParametricoEvaluadoV1


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{DESCARTE_PARAMETRICO_RESUELTO}.v1.schema.json": SobreDescarteParametricoResueltoV1,
    f"{EVENTO_PARAMETRICO_EVALUADO}.v1.schema.json": SobreEventoParametricoEvaluadoV1,
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
