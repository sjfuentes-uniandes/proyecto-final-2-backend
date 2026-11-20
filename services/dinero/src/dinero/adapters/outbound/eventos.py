"""Sobre y payloads v1 de los eventos de dinero (HU-W16, 16.10; ADR 0013, decisión 2).

- RecaudoConfirmado: el primer cobro de la prima quedó CONFIRMADO. Lo consumen `polizas-recaudos`,
  `avisos` y `auditoria`.
- RecaudoRechazado: el proveedor rechazó el cobro; `motivo` es un código. Lo consumen
  `polizas-recaudos` y `auditoria`.

HU-W23 (ADR 0015; ver `docs/hu-w23-indemnizacion.md` §7):

- IndemnizacionConfirmada: la pasarela confirmó la indemnización. Los consumen `siniestros` (cola
  `siniestros-indemnizaciones`, prefijo `Indemnizacion`, DS3-25), `consulta` y `auditoria`.
- IndemnizacionRechazada: la pasarela rechazó la orden; `motivo` es un código. Los mismos
  consumidores. El siniestro queda con el «pago por gestionar» (AC4).

Plan B de H08 (DS3-26): los dos llevan `intentos` y `duplicadosDescartados`, que `siniestros`
guarda desde ellos. `aggregateId` = `indemnizacionId`.

`aggregateId` (recaudo) = `recaudoId`. Solo ids, códigos y el monto como texto decimal: ningún dato del pagador
(ni `pagadorRef`), de tarjeta ni de cuenta. `type` es el atributo `eventType` que filtra las colas
(`polizas-recaudos`, prefijo `Recaudo`; `avisos`, `RecaudoConfirmado`).

Los esquemas publicados se regeneran con:
    uv run python -m dinero.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from dinero.domain.recaudo.modelos import CODIGO, LARGO_TRANSACCION

RECAUDO_CONFIRMADO = "RecaudoConfirmado"
RECAUDO_RECHAZADO = "RecaudoRechazado"
INDEMNIZACION_CONFIRMADA = "IndemnizacionConfirmada"
INDEMNIZACION_RECHAZADA = "IndemnizacionRechazada"

REFERENCIA_PAGO = r"^IND-88-[0-9]{4}-[0-9]{4,}-[0-9]{2}$"
MONTO = r"^[0-9]+(\.[0-9]{1,2})?$"


class _Evento(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class MontoV1(_Evento):
    """El monto cobrado; el valor es texto decimal con a lo sumo 2 decimales (nunca un número)."""

    valor: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,2})?$")]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class RecaudoConfirmadoV1(_Evento):
    """El primer cobro de la prima quedó confirmado. Sin datos del pagador."""

    recaudo_id: UUID
    clave_recaudo: UUID
    cotizacion_id: UUID
    monto: MontoV1
    transaccion_id: Annotated[str, Field(min_length=1, max_length=LARGO_TRANSACCION)]
    confirmado_en: datetime


class RecaudoRechazadoV1(_Evento):
    """El proveedor rechazó el cobro; `motivo` es un código. Sin datos del pagador."""

    recaudo_id: UUID
    clave_recaudo: UUID
    cotizacion_id: UUID
    motivo: Annotated[str, Field(pattern=CODIGO.pattern)]
    rechazado_en: datetime


class IndemnizacionConfirmadaV1(_Evento):
    """La pasarela confirmó la indemnización. Sin datos de la cuenta del beneficiario."""

    indemnizacion_id: UUID
    siniestro_id: UUID
    referencia: Annotated[str, Field(pattern=REFERENCIA_PAGO)]
    monto: Annotated[str, Field(pattern=MONTO)]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    transaccion_id: Annotated[str, Field(min_length=1, max_length=LARGO_TRANSACCION)]
    intentos: Annotated[int, Field(ge=1)]
    duplicados_descartados: Annotated[int, Field(ge=0)]
    confirmada_en: datetime


class IndemnizacionRechazadaV1(_Evento):
    """La pasarela rechazó la indemnización; `motivo` es un código. Sin datos de la cuenta."""

    indemnizacion_id: UUID
    siniestro_id: UUID
    referencia: Annotated[str, Field(pattern=REFERENCIA_PAGO)]
    motivo: Annotated[str, Field(pattern=CODIGO.pattern)]
    intentos: Annotated[int, Field(ge=1)]
    duplicados_descartados: Annotated[int, Field(ge=0)]
    rechazada_en: datetime


class SobreEvento[
    P: RecaudoConfirmadoV1
    | RecaudoRechazadoV1
    | IndemnizacionConfirmadaV1
    | IndemnizacionRechazadaV1
](_Evento):
    """Sobre versionado: el consumidor elige el esquema del payload por `type` y `version`."""

    event_id: UUID
    aggregate_id: UUID
    type: str
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: P


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{RECAUDO_CONFIRMADO}.v1.schema.json": SobreEvento[RecaudoConfirmadoV1],
    f"{RECAUDO_RECHAZADO}.v1.schema.json": SobreEvento[RecaudoRechazadoV1],
    f"{INDEMNIZACION_CONFIRMADA}.v1.schema.json": SobreEvento[IndemnizacionConfirmadaV1],
    f"{INDEMNIZACION_RECHAZADA}.v1.schema.json": SobreEvento[IndemnizacionRechazadaV1],
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
