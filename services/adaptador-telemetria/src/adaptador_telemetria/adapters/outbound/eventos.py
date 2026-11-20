"""Sobre y payload v1 de `EventoParametricoObservado` (HU-W21, W21-B02).

Lo publica `adaptador-telemetria` por su Outbox, en la misma transacción que registra el evento
recibido. Lo consume `pagos-parametricos` (cola `parametricos-eventos`, filtro exacto
`EventoParametricoObservado`, HU-S03) y `auditoria` (todos los eventos).

- `eventId` del sobre = `aggregateId` = `payload.eventoId`: el UUIDv5 determinista de
  `(fuente, idExterno)` (DS3-16). Así el reproceso de HU-W24 (guía §7) reconstruye el mismo sobre
  desde el original archivado, y el Inbox por `eventId` deduplica.
- `occurredAt` = `payload.recibidoEn`.
- `archivoRef` es la clave del original en el bucket de evidencias
  (`telemetria/<AAAA>/<MM>/<DD>/<eventoId>.json`, con la fecha UTC de `recibidoEn`); no es una URL
  y no lleva datos sensibles.
- Solo el evento canónico: ni el cuerpo crudo, ni la firma, ni los encabezados, ni `ubicacion`
  (minimización: la evaluación no la usa).

Los esquemas publicados se regeneran con:
    uv run python -m adaptador_telemetria.adapters.outbound.eventos
"""

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from adaptador_telemetria.domain.telemetria.modelos import EventoCanonico

EVENTO_PARAMETRICO_OBSERVADO = "EventoParametricoObservado"

# Los mismos patrones que `EventoExterno` (contrato OpenAPI) y que `EventoParametricoEvaluado`.
FUENTE = r"^[A-Z][A-Z0-9-]{1,31}$"
IDENTIFICADOR = r"^[A-Za-z0-9._:-]{1,64}$"
ZONA = r"^[A-Z0-9]{2,16}$"
# Texto decimal (DS2-16), nunca número: [0-9] y no \d, que admite dígitos Unicode.
VALOR_OBSERVADO = r"^[0-9]{1,9}(\.[0-9]{1,3})?$"
ARCHIVO_REF = (
    r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*/[0-9]{4}/[0-9]{2}/[0-9]{2}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$"
)


class TipoEvento(StrEnum):
    LLUVIA_ACUMULADA = "LLUVIA_ACUMULADA"
    RETRASO_VUELO = "RETRASO_VUELO"


class Unidad(StrEnum):
    MM = "mm"
    MIN = "min"


class _Evento(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class EventoParametricoObservadoV1(_Evento):
    """Un evento externo auténtico y con contrato válido; todavía sin evaluar."""

    evento_id: UUID = Field(description="UUIDv5 de (fuente, idExterno); es también el eventId")
    fuente: Annotated[str, Field(pattern=FUENTE, examples=["IDEAM-SIM"])]
    id_externo: Annotated[str, Field(pattern=IDENTIFICADOR, examples=["IDEAM-2026-1110-0001"])]
    evento_origen: Annotated[str, Field(pattern=IDENTIFICADOR, examples=["EVT-2026-0913"])]
    tipo_evento: TipoEvento
    valor_observado: Annotated[str, Field(pattern=VALOR_OBSERVADO, examples=["135.5"])]
    unidad: Unidad
    zona: Annotated[str, Field(pattern=ZONA, examples=["11001"])]
    observado_en: AwareDatetime
    recibido_en: AwareDatetime
    archivo_ref: Annotated[
        str,
        Field(
            pattern=ARCHIVO_REF,
            description="Clave del original archivado en el bucket de evidencias",
        ),
    ]


class SobreEventoParametricoObservadoV1(_Evento):
    """Sobre versionado con `type` fijo: es el atributo `eventType` del mensaje (HU-S03)."""

    event_id: UUID
    aggregate_id: UUID
    type: Literal["EventoParametricoObservado"]
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: EventoParametricoObservadoV1


def sobre_de(
    evento: EventoCanonico, recibido_en: datetime, archivo_ref: str, correlation_id: str
) -> SobreEventoParametricoObservadoV1:
    """El sobre del evento recibido: `eventId` y `aggregateId` son el `eventoId`."""
    evento_id = evento.evento_id
    return SobreEventoParametricoObservadoV1(
        event_id=evento_id,
        aggregate_id=evento_id,
        type=EVENTO_PARAMETRICO_OBSERVADO,
        version=1,
        occurred_at=recibido_en,
        correlation_id=correlation_id,
        payload=EventoParametricoObservadoV1(
            evento_id=evento_id,
            fuente=evento.fuente,
            id_externo=evento.id_externo,
            evento_origen=evento.evento_origen,
            tipo_evento=TipoEvento(evento.tipo_evento),
            valor_observado=evento.valor_observado,
            unidad=Unidad(evento.unidad),
            zona=evento.zona,
            observado_en=evento.observado_en,
            recibido_en=recibido_en,
            archivo_ref=archivo_ref,
        ),
    )


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{EVENTO_PARAMETRICO_OBSERVADO}.v1.schema.json": SobreEventoParametricoObservadoV1,
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
