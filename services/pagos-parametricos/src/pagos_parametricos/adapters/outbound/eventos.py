"""Sobre y payload v1 de los eventos de `pagos-parametricos` que define HU-W24.

- DescarteParametricoResuelto (HU-W24, decisión B2): un analista resolvió un mensaje de la cola de
  descarte de `parametricos-eventos`: lo reprocesó tal cual, lo corrigió y reprocesó, o lo descartó.
  Va solo a `auditoria` (cola sin filtro; `modelo_informacion.puml`: `CanalEventos -->
  HistorialAuditoria`), que guarda el sobre entero: el **original** y, si se corrigió, la
  **corrección**, como pide H11. `aggregateId` = el `eventId` del evento resuelto (si el original
  no era un sobre, el `MessageId`, que SQS genera como uuid). `actorId` es el `sub` del analista.
  El contenido es el del evento de telemetría, sin datos personales.

`EventoParametricoEvaluado` (y el resto de lo que publique este servicio) es de HU-W21.

Los esquemas publicados se regeneran con:
    uv run python -m pagos_parametricos.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from pagos_parametricos.domain.descarte import Accion, Causa

DESCARTE_PARAMETRICO_RESUELTO = "DescarteParametricoResuelto"


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


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{DESCARTE_PARAMETRICO_RESUELTO}.v1.schema.json": SobreDescarteParametricoResueltoV1,
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
