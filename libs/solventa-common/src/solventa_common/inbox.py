"""Inbox y consumidor SQS (HU-W16, 16.5; ADR 0013, decisiones 2, 4 y 8).

Consume una cola SQS suscrita al tópico de negocio con entrega en crudo: el cuerpo es el sobre de
`contracts/events/` y el atributo `eventType` (= `type`) es el que filtra la suscripción. Deduplica
por `eventId` con una tabla de inbox en la base del servicio.

- Un ciclo (`ConsumidorInbox.ejecutar_una_vez`) recibe un lote con espera larga y procesa sus
  mensajes **en secuencia**. Por cada mensaje, en una transacción: inserta el `eventId` en el inbox
  (`ON CONFLICT DO NOTHING`) y, si es nuevo, llama al manejador con esa misma transacción. Después
  del commit, ejecuta la continuación, si la hay, y borra el mensaje.
- **Dos modos de manejador**, con una sola firma: `manejador(evento, tx) -> Continuacion | None`.
  - (a) Todo en una transacción: el manejador escribe su efecto con `tx` (en `InboxPg`,
    `tx.conexion`) y devuelve `None`. La fila del inbox y el efecto se confirman juntos.
  - (b) Primero el commit y después el efecto externo: el manejador escribe su parte transaccional
    con `tx` y devuelve una función asíncrona sin argumentos. El consumidor la ejecuta **después**
    del commit y **antes** de borrar el mensaje. Si falla, el mensaje no se borra; la reentrega
    encuentra el `eventId` en el inbox y se trata como duplicado: la continuación **no se repite**.
    Recuperar un efecto externo que no terminó (por ejemplo, un cobro `PENDIENTE`) es cosa del
    servicio (la conciliación de la ADR 0013, decisión 5), no del consumidor.
- **Duplicado** (`eventId` ya en el inbox): no se llama al manejador; se hace commit y se borra.
- **Fallo** del manejador o de la transacción: rollback (sin fila en el inbox) y el mensaje no se
  borra. Vuelve tras la visibilidad de la cola; tras `max_receive_count` recepciones (5 en
  `infra/`), SQS lo manda a la DLQ.
- **Mensaje envenenado** (el cuerpo no es un objeto JSON, o el sobre no trae un `eventId` uuid o un
  `type` que sea un nombre de evento): no se llama al manejador ni se borra; acaba en la DLQ
  (decisión P16-4). El log lleva solo `MessageId`, `ApproximateReceiveCount` y el motivo, nunca el
  cuerpo. La validación contra el esquema del evento es del manejador: si no valida, lanza
  `EventoInvalido` y se registra igual, con el motivo `esquema`.
- Dos consumidores con el mismo `eventId` a la vez: el `INSERT` del segundo espera al commit del
  primero y lo ve como duplicado (clave primaria del inbox). El efecto ocurre una sola vez.
- **Visibilidad:** el consumidor **no** la extiende. El manejador y la continuación de cada mensaje
  deben terminar muy por debajo de la visibilidad de su cola (`dinero-aceptaciones` 60 s,
  `polizas-recaudos` 120 s), y como el lote va en secuencia, también `max_mensajes` veces su
  duración. Si no, el mensaje reaparece mientras se procesa; el inbox evita el doble efecto, pero se
  pierde trabajo.
- Telemetría: `registrar("inbox.consumir", …)` por mensaje, con el `correlationId` del sobre en el
  contexto (también para el manejador y la continuación) y el desenlace en `outcome`. Si el atributo
  `eventType` no coincide con el `type` del sobre, un WARNING; el mensaje se procesa por su `type`.
- No lee variables de entorno. boto3 es síncrono: se llama en un hilo. El timeout de lectura del
  cliente SQS supera la espera larga (`cliente_sqs` lo garantiza y `ReceptorSqs` lo comprueba).
- La parada (`ejecutar`) se atiende entre ciclos: puede tardar una espera larga más el lote.

Tabla recomendada (cada servicio la crea en su migración; las pruebas de módulo usan este DDL):

```sql
CREATE TABLE IF NOT EXISTS inbox (
  event_id uuid PRIMARY KEY,
  event_type text NOT NULL,
  recibido_en timestamptz NOT NULL
);
```

`event_id` es la clave de deduplicación. `event_type` y `recibido_en` sirven para operar (qué se
consumió y cuándo) y para una purga futura por antigüedad. No guarda el sobre ni el payload: sin PII
ni crecimiento por el tamaño del evento.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard
from uuid import UUID

from psycopg import AsyncConnection, sql

from solventa_common import correlation
from solventa_common.outbox import ATRIBUTO_TIPO, describir_error, espera_tras_fallos
from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_OK, registrar

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

TABLA = "inbox"
# Espera larga de SQS (máximo 20 s) y margen del timeout de lectura sobre ella.
ESPERA_LARGA_S = 20
MARGEN_LECTURA_S = 5.0
# Lote en secuencia: con el corte de 5 s de pagos, 5 mensajes caben de sobra en los 60 s de
# `dinero-aceptaciones`.
MAX_MENSAJES = 5
TIMEOUT_CONEXION_S = 1.0
INTENTOS_SQS = 2
INTERVALO_S = 1.0
INTERVALO_MAX_S = 60.0

OPERACION = "inbox.consumir"
MENSAJE_TIPO_DISTINTO = "inbox: el atributo eventType no coincide con el type del sobre"
MENSAJE_RECEPCION_FALLIDA = "inbox: la recepción falló"
MENSAJE_BORRADO_FALLIDO = "inbox: no se pudo borrar el mensaje"

MOTIVO_JSON = "json_invalido"
MOTIVO_EVENT_ID = "event_id_invalido"
MOTIVO_TYPE = "type_invalido"
MOTIVO_ESQUEMA = "esquema"

NUEVO = "nuevo"
DUPLICADO = "duplicado"
ENVENENADO = "envenenado"
FALLO_TRANSACCION = "fallo_transaccion"
FALLO_CONTINUACION = "fallo_continuacion"

# Un nombre de evento (OfertaAceptada, RecaudoConfirmado...); otra cosa no se registra, por si trae
# datos.
_NOMBRE_EVENTO = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")

logger = logging.getLogger("solventa.inbox")


@dataclass(frozen=True)
class MensajeSqs:
    """Un mensaje recibido, con lo que el consumidor usa de SQS."""

    message_id: str
    receipt_handle: str
    cuerpo: str
    tipo_atributo: str | None
    recepciones: int | None


@dataclass(frozen=True)
class EventoRecibido:
    """Lo que recibe el manejador. `sobre` está decodificado sin float (decimales como `Decimal`);
    `cuerpo` es el texto tal cual, para validarlo con el modelo del evento."""

    event_id: UUID
    event_type: str
    correlation_id: str | None
    sobre: Mapping[str, Any]
    cuerpo: str
    message_id: str
    recepciones: int | None


class SobreInvalido(Exception):
    """El cuerpo no es un sobre (mensaje envenenado). Lleva solo el motivo, nunca el cuerpo."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class EventoInvalido(Exception):
    """La lanza el manejador si el evento no valida contra su esquema: rollback, sin borrar.

    Excepción marcadora: no lleva datos del evento.
    """


def _es_nombre_evento(valor: object) -> TypeGuard[str]:
    return isinstance(valor, str) and _NOMBRE_EVENTO.fullmatch(valor) is not None


def leer_sobre(mensaje: MensajeSqs) -> EventoRecibido:
    """Decodifica y comprueba lo mínimo del sobre: `eventId` uuid y `type`. Lanza `SobreInvalido`."""
    try:
        sobre = json.loads(mensaje.cuerpo, parse_float=Decimal)
    except ValueError:
        raise SobreInvalido(MOTIVO_JSON) from None
    if not isinstance(sobre, dict):
        raise SobreInvalido(MOTIVO_JSON)
    try:
        event_id = UUID(sobre["eventId"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SobreInvalido(MOTIVO_EVENT_ID) from None
    tipo = sobre.get("type")
    if not _es_nombre_evento(tipo):
        raise SobreInvalido(MOTIVO_TYPE)
    correlation_id = sobre.get("correlationId")
    if not isinstance(correlation_id, str) or not correlation.es_valido(correlation_id):
        correlation_id = None
    return EventoRecibido(
        event_id=event_id,
        event_type=tipo,
        correlation_id=correlation_id,
        sobre=sobre,
        cuerpo=mensaje.cuerpo,
        message_id=mensaje.message_id,
        recepciones=mensaje.recepciones,
    )


# --- Puertos -----------------------------------------------------------------------------------
class TransaccionInbox(Protocol):
    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        """Inserta el `eventId`; False si ya estaba (duplicado)."""
        ...


# Modo (b): el efecto externo, después del commit y antes de borrar el mensaje.
Continuacion = Callable[[], Awaitable[None]]
# Recibe la transacción del inbox (`T`) para escribir su efecto en ella.
type Manejador[T: TransaccionInbox] = Callable[[EventoRecibido, T], Awaitable[Continuacion | None]]


class RepositorioInbox[T: TransaccionInbox](Protocol):
    def transaccion(self) -> AbstractAsyncContextManager[T]: ...


class Receptor(Protocol):
    async def recibir(self) -> list[MensajeSqs]: ...

    async def borrar(self, mensaje: MensajeSqs) -> None: ...


# --- Adaptador PostgreSQL ------------------------------------------------------------------------
_REGISTRAR = sql.SQL(
    "INSERT INTO {tabla} (event_id, event_type, recibido_en) VALUES (%s, %s, %s) "
    "ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
)


@dataclass(frozen=True)
class TransaccionInboxPg:
    """La transacción del mensaje: el manejador escribe su efecto con `conexion`."""

    conexion: AsyncConnection[Any]
    tabla: sql.Identifier

    async def registrar(self, event_id: UUID, event_type: str, recibido_en: datetime) -> bool:
        cursor = await self.conexion.execute(
            _REGISTRAR.format(tabla=self.tabla), (event_id, event_type, recibido_en)
        )
        return await cursor.fetchone() is not None


class InboxPg:
    """Inbox sobre el pool del servicio; una transacción por mensaje."""

    def __init__(self, pool: "AsyncConnectionPool[Any]", tabla: str = TABLA) -> None:
        self._pool = pool
        self._tabla = sql.Identifier(tabla)

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionInboxPg]:
        async with self._pool.connection() as conexion, conexion.transaction():
            yield TransaccionInboxPg(conexion, self._tabla)


# --- Adaptador SQS --------------------------------------------------------------------------------
def _validar_espera_larga(espera_larga_s: int) -> None:
    if not 1 <= espera_larga_s <= 20:
        raise ValueError("espera_larga_s debe estar entre 1 y 20 s (límite de SQS)")


def cliente_sqs(
    region: str,
    *,
    endpoint_url: str | None = None,
    espera_larga_s: int = ESPERA_LARGA_S,
    margen_lectura_s: float = MARGEN_LECTURA_S,
    timeout_conexion_s: float = TIMEOUT_CONEXION_S,
    intentos: int = INTENTOS_SQS,
) -> Any:
    """Cliente SQS de boto3 cuyo timeout de lectura es la espera larga más un margen positivo.

    Con un timeout menor o igual, cada recepción vacía cortaría por timeout. `intentos` incluye el
    primero.
    """
    _validar_espera_larga(espera_larga_s)
    if margen_lectura_s <= 0:
        raise ValueError("margen_lectura_s debe ser positivo")
    import boto3
    from botocore.config import Config

    return boto3.client(
        "sqs",
        region_name=region,
        endpoint_url=endpoint_url,
        config=Config(
            connect_timeout=timeout_conexion_s,
            read_timeout=espera_larga_s + margen_lectura_s,
            retries={"total_max_attempts": intentos, "mode": "standard"},
        ),
    )


def _recepciones(mensaje: Mapping[str, Any]) -> int | None:
    valor = (mensaje.get("Attributes") or {}).get("ApproximateReceiveCount")
    return int(valor) if isinstance(valor, str) and valor.isdigit() else None


class ReceptorSqs:
    """Recibe con espera larga y borra por `ReceiptHandle`, en un hilo (boto3 es síncrono)."""

    def __init__(
        self,
        cliente: Any,
        cola_url: str,
        *,
        espera_larga_s: int = ESPERA_LARGA_S,
        max_mensajes: int = MAX_MENSAJES,
    ) -> None:
        _validar_espera_larga(espera_larga_s)
        if not 1 <= max_mensajes <= 10:
            raise ValueError("max_mensajes debe estar entre 1 y 10 (límite de SQS)")
        timeout_lectura = cliente.meta.config.read_timeout
        if not isinstance(timeout_lectura, int | float) or timeout_lectura <= espera_larga_s:
            raise ValueError("el timeout de lectura del cliente debe superar la espera larga")
        self._cliente = cliente
        self._cola_url = cola_url
        self.espera_larga_s = espera_larga_s
        self.max_mensajes = max_mensajes

    async def recibir(self) -> list[MensajeSqs]:
        respuesta = await asyncio.to_thread(
            self._cliente.receive_message,
            QueueUrl=self._cola_url,
            MaxNumberOfMessages=self.max_mensajes,
            WaitTimeSeconds=self.espera_larga_s,
            MessageAttributeNames=[ATRIBUTO_TIPO],
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        return [
            MensajeSqs(
                message_id=mensaje["MessageId"],
                receipt_handle=mensaje["ReceiptHandle"],
                cuerpo=mensaje["Body"],
                tipo_atributo=(mensaje.get("MessageAttributes") or {})
                .get(ATRIBUTO_TIPO, {})
                .get("StringValue"),
                recepciones=_recepciones(mensaje),
            )
            for mensaje in respuesta.get("Messages", [])
        ]

    async def borrar(self, mensaje: MensajeSqs) -> None:
        await asyncio.to_thread(
            self._cliente.delete_message,
            QueueUrl=self._cola_url,
            ReceiptHandle=mensaje.receipt_handle,
        )


# --- Consumidor ------------------------------------------------------------------------------------
def _ahora() -> datetime:
    return datetime.now(UTC)


async def _esperar(parada: asyncio.Event, segundos: float) -> None:
    try:
        await asyncio.wait_for(parada.wait(), segundos)
    except TimeoutError:
        pass


@dataclass(frozen=True)
class _Desenlace:
    outcome: str
    error_type: str | None = None
    reason: str | None = None

    @property
    def procesado(self) -> bool:
        return self.outcome in (NUEVO, DUPLICADO)


class ConsumidorInbox[T: TransaccionInbox]:
    """Consume una cola con inbox: un ciclo (`ejecutar_una_vez`) o en bucle (`ejecutar`)."""

    def __init__(
        self,
        receptor: Receptor,
        repositorio: RepositorioInbox[T],
        manejador: Manejador[T],
        *,
        intervalo_s: float = INTERVALO_S,
        intervalo_max_s: float = INTERVALO_MAX_S,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        self._receptor = receptor
        self._repositorio = repositorio
        self._manejador = manejador
        self._intervalo_s = intervalo_s
        self._intervalo_max_s = intervalo_max_s
        self._reloj = reloj

    async def ejecutar_una_vez(self) -> int:
        """Recibe un lote y lo procesa en secuencia; devuelve cuántos mensajes procesó (nuevos y
        duplicados). Un fallo de recepción se propaga."""
        procesados = 0
        for mensaje in await self._receptor.recibir():
            procesados += await self._procesar(mensaje)
        return procesados

    async def ejecutar(self, parada: asyncio.Event) -> None:
        """Repite ciclos hasta `parada`. Sin pausa tras una recepción (la espera larga ya marca el
        ritmo); tras fallos de recepción seguidos, una espera que crece hasta `intervalo_max_s`."""
        fallos_seguidos = 0
        while not parada.is_set():
            try:
                await self.ejecutar_una_vez()
            except Exception as error:  # noqa: BLE001 - el siguiente ciclo lo reintenta
                fallos_seguidos += 1
                logger.error(
                    MENSAJE_RECEPCION_FALLIDA, extra={"error_type": describir_error(error)}
                )
                espera = espera_tras_fallos(
                    self._intervalo_s, self._intervalo_max_s, fallos_seguidos - 1
                )
                await _esperar(parada, espera)
            else:
                fallos_seguidos = 0

    async def _procesar(self, mensaje: MensajeSqs) -> bool:
        """Procesa un mensaje y lo mide; True si quedó procesado (y se intentó borrar)."""
        inicio = time.perf_counter()
        try:
            evento = leer_sobre(mensaje)
        except SobreInvalido as invalido:
            self._medir(inicio, mensaje, None, _Desenlace(ENVENENADO, reason=invalido.motivo))
            return False
        token = correlation.establecer(evento.correlation_id) if evento.correlation_id else None
        try:
            if mensaje.tipo_atributo != evento.event_type:
                logger.warning(
                    MENSAJE_TIPO_DISTINTO,
                    extra={
                        "message_id": mensaje.message_id,
                        "event_id": str(evento.event_id),
                        "event_type": evento.event_type,
                        "attribute_event_type": mensaje.tipo_atributo
                        if _es_nombre_evento(mensaje.tipo_atributo)
                        else None,
                    },
                )
            desenlace = await self._aplicar(evento)
            if desenlace.procesado:
                await self._borrar(mensaje)
            self._medir(inicio, mensaje, evento, desenlace)
        finally:
            if token is not None:
                correlation.restablecer(token)
        return desenlace.procesado

    async def _aplicar(self, evento: EventoRecibido) -> _Desenlace:
        """Inbox y manejador en una transacción; después, la continuación (modo b)."""
        continuacion: Continuacion | None = None
        try:
            async with self._repositorio.transaccion() as tx:
                nuevo = await tx.registrar(evento.event_id, evento.event_type, self._reloj())
                if nuevo:
                    continuacion = await self._manejador(evento, tx)
        except EventoInvalido:
            return _Desenlace(ENVENENADO, reason=MOTIVO_ESQUEMA)
        except Exception as error:  # noqa: BLE001 - el mensaje vuelve tras la visibilidad
            return _Desenlace(FALLO_TRANSACCION, error_type=describir_error(error))
        if not nuevo:
            return _Desenlace(DUPLICADO)
        if continuacion is not None:
            try:
                await continuacion()
            except Exception as error:  # noqa: BLE001 - la reentrega será un duplicado
                return _Desenlace(FALLO_CONTINUACION, error_type=describir_error(error))
        return _Desenlace(NUEVO)

    async def _borrar(self, mensaje: MensajeSqs) -> None:
        try:
            await self._receptor.borrar(mensaje)
        except Exception as error:  # noqa: BLE001 - la reentrega será un duplicado
            logger.error(
                MENSAJE_BORRADO_FALLIDO,
                extra={"message_id": mensaje.message_id, "error_type": describir_error(error)},
            )

    @staticmethod
    def _medir(
        inicio: float,
        mensaje: MensajeSqs,
        evento: EventoRecibido | None,
        desenlace: _Desenlace,
    ) -> None:
        extra: dict[str, Any] = {
            "message_id": mensaje.message_id,
            "receive_count": mensaje.recepciones,
            "outcome": desenlace.outcome,
        }
        if evento is not None:
            extra |= {"event_id": str(evento.event_id), "event_type": evento.event_type}
        if desenlace.error_type is not None:
            extra["error_type"] = desenlace.error_type
        if desenlace.reason is not None:
            extra["reason"] = desenlace.reason
        registrar(
            OPERACION,
            (time.perf_counter() - inicio) * 1000,
            RESULTADO_OK if desenlace.procesado else RESULTADO_ERROR,
            **extra,
        )
