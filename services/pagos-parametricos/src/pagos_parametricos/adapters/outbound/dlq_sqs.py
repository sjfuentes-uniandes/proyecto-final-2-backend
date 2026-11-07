"""La DLQ de `parametricos-eventos` y su cola origen en SQS (HU-W24).

Respaldo: `diagrama_despliegue_ecs_fargate.puml` (`SQS -right-> DLQ : reintentos agotados`) y
`modelo_componentes.puml` (8), "Tras reintentos acotados, el mensaje pasa a revisión". El reenvío a
la cola origen es el reintento que la DLQ deja pendiente (`diagrama_componentes_responsabilidades
.puml`: "SNS / SQS: evento asíncrono durable y reprocesable"). Va directo a la cola, no al tópico,
para que solo lo reciba el grupo de `pagos-parametricos`.

- `recibir`: sin espera larga (la DLQ no recibe tráfico nuevo) y con `SentTimestamp` (SQS lo
  conserva al mover el mensaje: es cuándo llegó el evento) y `ApproximateReceiveCount`.
- `reenviar`: el cuerpo tal cual (o corregido) con el atributo `eventType` que filtra la
  suscripción, para que el consumidor lo lea igual que si viniera de SNS.
- boto3 es síncrono: se llama en un hilo.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from solventa_common.outbox import ATRIBUTO_TIPO

from pagos_parametricos.application.descartes import MensajeDlq

SEGUNDOS_POR_DIA = 86_400


def _enviado_en(atributos: dict[str, Any]) -> str | None:
    valor = atributos.get("SentTimestamp")
    if not isinstance(valor, str) or not valor.isdigit():
        return None
    return datetime.fromtimestamp(int(valor) / 1000, UTC).isoformat()


def _recepciones(atributos: dict[str, Any]) -> int | None:
    valor = atributos.get("ApproximateReceiveCount")
    return int(valor) if isinstance(valor, str) and valor.isdigit() else None


def dlq_de(cola_url: str) -> str:
    """La URL de la DLQ: `<cola>-dlq` (`infra/platform/messaging.tf`)."""
    return f"{cola_url.rstrip('/')}-dlq"


class DlqSqs:
    def __init__(self, cliente: Any, cola_url: str, dlq_url: str | None = None) -> None:
        self._cliente = cliente
        self._cola_url = cola_url
        self._dlq_url = dlq_url or dlq_de(cola_url)

    async def recibir(self, maximo: int, visibilidad_s: int) -> list[MensajeDlq]:
        respuesta = await asyncio.to_thread(
            self._cliente.receive_message,
            QueueUrl=self._dlq_url,
            MaxNumberOfMessages=max(1, min(maximo, 10)),
            WaitTimeSeconds=0,
            VisibilityTimeout=visibilidad_s,
            MessageAttributeNames=[ATRIBUTO_TIPO],
            MessageSystemAttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        )
        mensajes = []
        for mensaje in respuesta.get("Messages", []):
            atributos = mensaje.get("Attributes") or {}
            mensajes.append(
                MensajeDlq(
                    message_id=mensaje["MessageId"],
                    receipt_handle=mensaje["ReceiptHandle"],
                    cuerpo=mensaje["Body"],
                    tipo_atributo=(mensaje.get("MessageAttributes") or {})
                    .get(ATRIBUTO_TIPO, {})
                    .get("StringValue"),
                    recepciones=_recepciones(atributos),
                    enviado_en=_enviado_en(atributos),
                )
            )
        return mensajes

    async def liberar(self, mensaje: MensajeDlq) -> None:
        await asyncio.to_thread(
            self._cliente.change_message_visibility,
            QueueUrl=self._dlq_url,
            ReceiptHandle=mensaje.receipt_handle,
            VisibilityTimeout=0,
        )

    async def borrar(self, mensaje: MensajeDlq) -> None:
        await asyncio.to_thread(
            self._cliente.delete_message,
            QueueUrl=self._dlq_url,
            ReceiptHandle=mensaje.receipt_handle,
        )

    async def reenviar(self, cuerpo: str, tipo: str | None) -> None:
        atributos = (
            {} if tipo is None else {ATRIBUTO_TIPO: {"DataType": "String", "StringValue": tipo}}
        )
        await asyncio.to_thread(
            self._cliente.send_message,
            QueueUrl=self._cola_url,
            MessageBody=cuerpo,
            MessageAttributes=atributos,
        )

    async def retencion_dias(self) -> int:
        respuesta = await asyncio.to_thread(
            self._cliente.get_queue_attributes,
            QueueUrl=self._dlq_url,
            AttributeNames=["MessageRetentionPeriod"],
        )
        segundos = int(respuesta["Attributes"]["MessageRetentionPeriod"])
        return segundos // SEGUNDOS_POR_DIA
