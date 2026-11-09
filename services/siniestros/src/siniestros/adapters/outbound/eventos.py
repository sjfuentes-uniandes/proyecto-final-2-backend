"""Prototipo de los eventos v1 de `siniestros` (W19-B03 y W20-B02). Se copia tal cual a
`services/siniestros/src/siniestros/adapters/outbound/eventos.py` en B2.

Eventos de W19 (todos nacen en el Outbox de `siniestros`, en la misma transacción que el cambio o
la consulta; `type` es el atributo `eventType` que filtra las colas):

- SiniestroEnEvaluacion: el caso quedó en evaluación de un analista: lo tomó (`motivo =
  ASIGNACION`) o venció el plazo del soporte y volvió a su bandeja (`motivo =
  PLAZO_SOPORTE_VENCIDO`, W20: cambio aditivo del enum, R-15). Empieza por `Siniestro`: lo proyecta
  `consulta` (M05).
- SiniestroConsultado: el analista abrió el detalle. Solo para `auditoria`.
- EvidenciaConsultada: se emitió una URL prefirmada. Solo `siniestroId`, `evidenciaId`, `actorId` y
  `expiraEn`: **nunca la URL ni la clave del objeto** (DS3-08). Solo para `auditoria`.
- AccesoSiniestroDenegado: un usuario sin el grupo (BFF_WEB) o sin el rol (SINIESTROS) intentó
  acceder (AC3). Solo para `auditoria`.

Eventos de W20 (la decisión del analista; el cambio de estado, el historial y la fila del Outbox
se escriben en la misma transacción). Todos empiezan por `Siniestro`:

- SiniestroAprobado (DS3-24, único para R5 y R7): lo consume `dinero` (HU-W23) para pagar. Lleva
  `referenciaPago` (`IND-<codigo>-01`) y `beneficiarioRef` (opaco: **nunca la cuenta**).
  `decididoPor` es null cuando lo aprueba un paramétrico (W22).
- SiniestroRechazado: la causal y quién decidió. **Sin la explicación al cliente** (texto libre).
- SiniestroSoporteSolicitado: documentos, canal y fin del plazo. **Sin el mensaje al cliente**.

`consulta` ignora sin error los tres de auditoría (CF-S3-04). Sin nombres de clientes ni de
analistas, sin cuentas, URL ni claves de objeto, y sin la explicación ni el mensaje al cliente.

Los esquemas publicados se regeneran con:
    uv run python -m siniestros.adapters.outbound.eventos
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

SINIESTRO_EN_EVALUACION = "SiniestroEnEvaluacion"
SINIESTRO_APROBADO = "SiniestroAprobado"
SINIESTRO_RECHAZADO = "SiniestroRechazado"
SINIESTRO_SOPORTE_SOLICITADO = "SiniestroSoporteSolicitado"
SINIESTRO_CONSULTADO = "SiniestroConsultado"
EVIDENCIA_CONSULTADA = "EvidenciaConsultada"
ACCESO_SINIESTRO_DENEGADO = "AccesoSiniestroDenegado"

CODIGO_SINIESTRO = r"^88-[0-9]{4}-[0-9]{4,}$"
MONTO = r"^\d+(\.\d+)?$"
REFERENCIA_PAGO = r"^IND-88-[0-9]{4}-[0-9]{4,}-01$"


class _Evento(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class SiniestroEnEvaluacionV1(_Evento):
    """El caso quedó asignado a un analista y pasó a EN_EVALUACION. `aggregateId` = `siniestroId`."""

    siniestro_id: UUID
    codigo: Annotated[str, Field(pattern=CODIGO_SINIESTRO)]
    poliza_id: UUID
    analista_id: UUID
    motivo: Literal["ASIGNACION", "PLAZO_SOPORTE_VENCIDO"]


class SiniestroAprobadoV1(_Evento):
    """El analista aprobó el caso (o nació APROBADO por un evento paramétrico) y se ordena el pago
    único. `aggregateId` = `siniestroId`. La cuenta del beneficiario no viaja: solo su referencia."""

    siniestro_id: UUID
    codigo: Annotated[str, Field(pattern=CODIGO_SINIESTRO)]
    origen: Literal["MOVIL", "PORTAL", "PARAMETRICO"]
    poliza_id: UUID
    cliente_id: UUID
    valor_indemnizar: Annotated[str, Field(pattern=MONTO)]
    moneda: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    referencia_pago: Annotated[str, Field(pattern=REFERENCIA_PAGO)]
    beneficiario_ref: Annotated[str, Field(min_length=1, max_length=128)]
    aprobado_en: datetime
    decidido_por: UUID | None


class SiniestroRechazadoV1(_Evento):
    """El analista rechazó el caso: no habilita pago. Sin la explicación al cliente.
    `aggregateId` = `siniestroId`."""

    siniestro_id: UUID
    codigo: Annotated[str, Field(pattern=CODIGO_SINIESTRO)]
    poliza_id: UUID
    causal: Literal[
        "POLIZA_NO_VIGENTE",
        "EVENTO_NO_CUBIERTO",
        "PERIODO_DE_CARENCIA",
        "AVISO_EXTEMPORANEO",
        "PRIMAS_EN_MORA",
        "EVIDENCIA_INCONSISTENTE",
        "EXCLUSION_CONTRACTUAL",
    ]
    decidido_por: UUID
    decidido_en: datetime


class SiniestroSoporteSolicitadoV1(_Evento):
    """El analista pidió soporte al cliente: el caso queda FALTA_SOPORTE y el reloj en pausa. Sin el
    mensaje al cliente. `aggregateId` = `siniestroId`."""

    siniestro_id: UUID
    codigo: Annotated[str, Field(pattern=CODIGO_SINIESTRO)]
    poliza_id: UUID
    documentos: Annotated[
        list[
            Literal[
                "CERTIFICADO_REPARACION",
                "COTIZACION_REPUESTOS",
                "DENUNCIA_AUTORIDAD",
                "PERITAJE",
            ]
        ],
        Field(min_length=1, max_length=4),
    ]
    canal: Literal["PUSH_Y_CORREO", "PUSH", "CORREO"]
    plazo_vence_en: datetime
    decidido_por: UUID


class SiniestroConsultadoV1(_Evento):
    """Un analista abrió el detalle del caso. `aggregateId` = `siniestroId`."""

    siniestro_id: UUID
    codigo: Annotated[str, Field(pattern=CODIGO_SINIESTRO)]
    actor_id: Annotated[str, Field(min_length=1, max_length=128)]


class EvidenciaConsultadaV1(_Evento):
    """Se emitió una URL prefirmada para una evidencia. Sin la URL ni la clave del objeto.
    `aggregateId` = `siniestroId`."""

    siniestro_id: UUID
    evidencia_id: UUID
    actor_id: Annotated[str, Field(min_length=1, max_length=128)]
    expira_en: datetime


class AccesoSiniestroDenegadoV1(_Evento):
    """Intento de acceso rechazado. `origen` dice quién lo detectó: `BFF_WEB` (el usuario no tiene el
    grupo `analistas-siniestros`) o `SINIESTROS` (falta el rol ANALISTA_SINIESTROS). `aggregateId`
    = `recursoId` si es un UUID; si no, el `eventId`."""

    actor_id: Annotated[str, Field(min_length=1, max_length=128)]
    rol: Annotated[str | None, Field(max_length=64)]
    recurso: Literal["BANDEJA", "RESUMEN", "SIGUIENTE", "SINIESTRO", "EVIDENCIA"]
    recurso_id: Annotated[str | None, Field(max_length=64)]
    motivo: Literal["SIN_GRUPO", "ROL_AUSENTE", "ROL_INVALIDO"]
    origen: Literal["BFF_WEB", "SINIESTROS"]


type PayloadSiniestro = (
    SiniestroEnEvaluacionV1
    | SiniestroAprobadoV1
    | SiniestroRechazadoV1
    | SiniestroSoporteSolicitadoV1
    | SiniestroConsultadoV1
    | EvidenciaConsultadaV1
    | AccesoSiniestroDenegadoV1
)


class SobreEvento[P: PayloadSiniestro](_Evento):
    """Sobre versionado: el consumidor elige el esquema del payload por `type` y `version`."""

    event_id: UUID
    aggregate_id: UUID
    type: str
    version: Literal[1]
    occurred_at: datetime
    correlation_id: str
    payload: P


ESQUEMAS: dict[str, type[BaseModel]] = {
    f"{SINIESTRO_EN_EVALUACION}.v1.schema.json": SobreEvento[SiniestroEnEvaluacionV1],
    f"{SINIESTRO_APROBADO}.v1.schema.json": SobreEvento[SiniestroAprobadoV1],
    f"{SINIESTRO_RECHAZADO}.v1.schema.json": SobreEvento[SiniestroRechazadoV1],
    f"{SINIESTRO_SOPORTE_SOLICITADO}.v1.schema.json": SobreEvento[SiniestroSoporteSolicitadoV1],
    f"{SINIESTRO_CONSULTADO}.v1.schema.json": SobreEvento[SiniestroConsultadoV1],
    f"{EVIDENCIA_CONSULTADA}.v1.schema.json": SobreEvento[EvidenciaConsultadaV1],
    f"{ACCESO_SINIESTRO_DENEGADO}.v1.schema.json": SobreEvento[AccesoSiniestroDenegadoV1],
}


if __name__ == "__main__":
    destino = Path("contracts/events")
    for nombre, modelo in ESQUEMAS.items():
        esquema = json.dumps(modelo.model_json_schema(by_alias=True), indent=2, sort_keys=True)
        (destino / nombre).write_text(esquema + "\n", encoding="utf-8")
        print(destino / nombre)
