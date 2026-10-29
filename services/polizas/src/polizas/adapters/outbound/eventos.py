"""Sobre y payloads v1 de los eventos de polizas.

- DecisionSuscripcionRegistrada (HU-W13, W13-B03): auditoría de la decisión; sin montos ni datos
  personales: resultado, reglas, versión y huellas.
- OfertaAceptada (HU-W15, W15-B01): habilita el recaudo (HU-W16). Lleva la prima que se cobra,
  pero ningún dato personal ni de medios de pago; `claveRecaudo` (= `aceptacionId`) es la clave
  idempotente estable para `dinero`. `type` es el atributo `eventType` que filtra la cola
  `dinero-aceptaciones`.
- PolizaEmitida (HU-W17, 17.2; ADR 0014, decisiones 6 y 7): la póliza quedó ACTIVA, con su
  documento firmado. `aggregateId` = `polizaId`. Referencia estable al documento (clave,
  `versionId` y huella) y a la firma; solo ids, montos como texto decimal y fechas: ningún nombre,
  documento de identidad ni dato de contacto. `type` es el atributo `eventType` que filtran
  `avisos` (exacto) y `consulta-proyeccion` (prefijo `Poliza`). Sin productor hasta la 17.8.

Los esquemas publicados se regeneran con:
    uv run python -m polizas.adapters.outbound.eventos
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from polizas.domain.aceptacion.modelos import CanalAceptacion
from polizas.domain.suscripcion.modelos import ResultadoDecision, TipoDecision

DECISION_SUSCRIPCION_REGISTRADA = "DecisionSuscripcionRegistrada"
OFERTA_ACEPTADA = "OfertaAceptada"
POLIZA_EMITIDA = "PolizaEmitida"

# Como `RecaudoConfirmado` (dinero) y la huella de la evidencia (aceptaciones).
_TEXTO_DECIMAL = r"^[0-9]+(\.[0-9]{1,2})?$"
_HUELLA = r"^sha256:[0-9a-f]{64}$"


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


class ProductoV1(_Evento):
    """El producto y la versión de la oferta aceptada."""

    id: str
    version: str


class VigenciaV1(_Evento):
    """Fechas inclusivas en America/Bogota: del día siguiente a la aceptación al anterior a su
    aniversario (ADR 0014, decisión 3)."""

    desde: date
    hasta: date


class DocumentoV1(_Evento):
    """La versión emitida del PDF en el bucket de pólizas: se lee por `clave` y `versionId`."""

    clave: Annotated[str, Field(min_length=1)]
    version_id: Annotated[str, Field(min_length=1)]
    huella: Annotated[str, Field(pattern=_HUELLA)]
    tamano_bytes: Annotated[int, Field(gt=0)]


class FirmaV1(_Evento):
    """La firma de Solventa sobre la huella del documento, no sobre el PDF."""

    firma_id: Annotated[str, Field(min_length=1)]
    sello_tiempo: AwareDatetime


class PolizaEmitidaV1(_Evento):
    """Póliza ACTIVA, una por aceptación; si se reentrega, apunta al mismo documento. Sin nombres,
    documentos de identidad ni datos de contacto."""

    poliza_id: UUID
    numero: Annotated[str, Field(pattern=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$")]
    cotizacion_id: UUID
    # Clave idempotente del recaudo (= aceptacionId): una póliza por aceptación.
    clave_recaudo: UUID
    cliente_id: UUID
    producto: ProductoV1
    vigencia: VigenciaV1
    prima: Annotated[str, Field(pattern=_TEXTO_DECIMAL)]
    suma_asegurada: Annotated[str, Field(pattern=_TEXTO_DECIMAL)]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    periodicidad: str
    documento: DocumentoV1
    firma: FirmaV1
    emitida_en: AwareDatetime


class SobreEvento[P: DecisionSuscripcionRegistradaV1 | OfertaAceptadaV1 | PolizaEmitidaV1](_Evento):
    """Sobre versionado: el consumidor elige el esquema del payload por `type` y `version`."""

    event_id: UUID
    aggregate_id: UUID
    type: str
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: P


class SobrePolizaEmitidaV1(SobreEvento[PolizaEmitidaV1]):
    """El sobre de siempre, con `type` fijo: los filtros de las colas dependen del nombre exacto."""

    type: Literal["PolizaEmitida"]


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{DECISION_SUSCRIPCION_REGISTRADA}.v1.schema.json": SobreEvento[
        DecisionSuscripcionRegistradaV1
    ],
    f"{OFERTA_ACEPTADA}.v1.schema.json": SobreEvento[OfertaAceptadaV1],
    f"{POLIZA_EMITIDA}.v1.schema.json": SobrePolizaEmitidaV1,
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
