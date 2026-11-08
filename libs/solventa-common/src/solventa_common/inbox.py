"""Inbox y consumidor SQS (HU-W16, 16.5; ADR 0013, decisiones 2, 4 y 8; HU-W24, contrapresión).

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
- **Visibilidad:** sin `visibilidad_s`, el consumidor **no** la extiende. El manejador y la
  continuación de cada mensaje deben terminar muy por debajo de la visibilidad de su cola
  (`dinero-aceptaciones` 60 s, `polizas-recaudos` 120 s), y como el lote va en secuencia, también
  `max_mensajes` veces su duración. Si no, el mensaje reaparece mientras se procesa; el inbox evita
  el doble efecto, pero se pierde trabajo. Con `visibilidad_s` (la de la cola), un latido renueva la
  de cada mensaje cada `visibilidad_s / 2`, desde que se recibe hasta que termina (HU-W24).
- **Contrapresión (HU-W24, AC1):** con `concurrencia` (`SQS_CONCURRENCIA` del servicio), el bucle
  procesa hasta `concurrencia` mensajes a la vez y nunca tiene más recibidos sin terminar: pide a SQS
  solo los huecos libres (`recibir_hasta`, como mucho `max_mensajes` del receptor; 10 es el lote de
  SQS) y, sin huecos, no recibe. Lo demás espera en la cola, que es la que retiene el trabajo
  pendiente. Sin `concurrencia`, el comportamiento de HU-W16: un lote en secuencia por ciclo.
- Telemetría: `operacion("inbox.consumir")` por mensaje (span, `OperationDuration`,
  `OperationErrors` y log), con el `correlationId` del sobre en el contexto (también para el
  manejador y la continuación) y el desenlace en `outcome`. Si el atributo `eventType` no coincide
  con el `type` del sobre, un WARNING; el mensaje se procesa por su `type`.
- **Causa del fallo:** `al_fallar(mensaje, evento, desenlace)`, opcional, se llama tras cada
  desenlace que no borra (envenenado, fallo de transacción o de continuación), fuera de la
  transacción. Sirve para anotar la causa que luego muestra la DLQ (HU-W24, H10): SQS conserva el
  `MessageId` al moverlo. Si falla, se registra y el mensaje sigue su curso.
- No lee variables de entorno. boto3 es síncrono: se llama en un hilo. El timeout de lectura del
  cliente SQS supera la espera larga (`cliente_sqs` lo garantiza y `ReceptorSqs` lo comprueba).
- **Parada (`ejecutar`; SIGTERM, que uvicorn convierte en el cierre del lifespan; HU-W25 drena con
  ella en el scale-in):** el consumidor deja de recibir. Con un `ReceptorControlado`, los mensajes
  recibidos que aún no empezaron se **liberan** (visibilidad 0: otra tarea los toma enseguida); los
  que están en curso tienen `plazo_parada_s` para terminar y, si no, se cancelan (rollback) y se
  liberan. Con contrapresión, una espera larga en curso se abandona: lo que traiga se libera al
  llegar, o reaparece tras la visibilidad de la cola; nunca se pierde. Sin `concurrencia`, la parada
  se atiende entre ciclos, como en HU-W16, y el resto del lote se libera en vez de procesarse.

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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection, sql

from solventa_common import correlation
from solventa_common.outbox import ATRIBUTO_TIPO, describir_error, espera_tras_fallos
from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_OK, operacion

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

TABLA = "inbox"
# Espera larga de SQS (máximo 20 s) y margen del timeout de lectura sobre ella.
ESPERA_LARGA_S = 20
MARGEN_LECTURA_S = 5.0
# Lote en secuencia: con el corte de 5 s de pagos, 5 mensajes caben de sobra en los 60 s de
# `dinero-aceptaciones`.
MAX_MENSAJES = 5
# HU-W24: el lote máximo de SQS, para los consumidores con contrapresión.
LOTE_SQS = 10
CONCURRENCIA_MAX = 100
PLAZO_PARADA_S = 10.0
TIMEOUT_CONEXION_S = 1.0
INTENTOS_SQS = 2
INTERVALO_S = 1.0
INTERVALO_MAX_S = 60.0

OPERACION = "inbox.consumir"
MENSAJE_TIPO_DISTINTO = "inbox: el atributo eventType no coincide con el type del sobre"
MENSAJE_RECEPCION_FALLIDA = "inbox: la recepción falló"
MENSAJE_BORRADO_FALLIDO = "inbox: no se pudo borrar el mensaje"
MENSAJE_LIBERADO = "inbox: mensaje liberado por la parada"
MENSAJE_LIBERACION_FALLIDA = "inbox: no se pudo liberar el mensaje"
MENSAJE_EXTENSION_FALLIDA = "inbox: no se pudo extender la visibilidad"
MENSAJE_REGISTRO_FALLO_FALLIDO = "inbox: no se pudo registrar el fallo del mensaje"

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


@runtime_checkable
class ReceptorControlado(Receptor, Protocol):
    """HU-W24: el receptor que admite contrapresión, latido de visibilidad y liberación."""

    async def recibir_hasta(self, maximo: int) -> list[MensajeSqs]:
        """Recibe como mucho `maximo` mensajes (y nunca más que su lote)."""
        ...

    async def extender(self, mensaje: MensajeSqs, segundos: int) -> None: ...

    async def liberar(self, mensaje: MensajeSqs) -> None:
        """Visibilidad 0: el mensaje vuelve a la cola de inmediato, sin esperar la visibilidad."""
        ...


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
        return await self.recibir_hasta(self.max_mensajes)

    async def recibir_hasta(self, maximo: int) -> list[MensajeSqs]:
        respuesta = await asyncio.to_thread(
            self._cliente.receive_message,
            QueueUrl=self._cola_url,
            MaxNumberOfMessages=max(1, min(maximo, self.max_mensajes)),
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

    async def extender(self, mensaje: MensajeSqs, segundos: int) -> None:
        await asyncio.to_thread(
            self._cliente.change_message_visibility,
            QueueUrl=self._cola_url,
            ReceiptHandle=mensaje.receipt_handle,
            VisibilityTimeout=segundos,
        )

    async def liberar(self, mensaje: MensajeSqs) -> None:
        await self.extender(mensaje, 0)


# --- Consumidor ------------------------------------------------------------------------------------
def _ahora() -> datetime:
    return datetime.now(UTC)


async def _esperar(parada: asyncio.Event, segundos: float) -> None:
    try:
        await asyncio.wait_for(parada.wait(), segundos)
    except TimeoutError:
        pass


async def _esperar_alguno(*eventos: asyncio.Event) -> None:
    esperas = [asyncio.ensure_future(evento.wait()) for evento in eventos]
    try:
        await asyncio.wait(esperas, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for espera in esperas:
            espera.cancel()


@dataclass(frozen=True)
class Desenlace:
    """Cómo terminó un mensaje; lo recibe `al_fallar` cuando no se borra."""

    outcome: str
    error_type: str | None = None
    reason: str | None = None

    @property
    def procesado(self) -> bool:
        return self.outcome in (NUEVO, DUPLICADO)


# Se llama tras un desenlace que no borra; `evento` es None si el sobre no se pudo leer.
AlFallar = Callable[[MensajeSqs, EventoRecibido | None, Desenlace], Awaitable[None]]


@dataclass
class _EnCurso:
    mensaje: MensajeSqs
    # Ya se intentó borrar: si se cancela, no hay nada que liberar.
    resuelto: bool = False


class ConsumidorInbox[T: TransaccionInbox]:
    """Consume una cola con inbox: un ciclo (`ejecutar_una_vez`) o en bucle (`ejecutar`).

    `concurrencia` activa la contrapresión y `visibilidad_s` el latido; los dos piden un
    `ReceptorControlado` (como `ReceptorSqs`).
    """

    def __init__(
        self,
        receptor: Receptor,
        repositorio: RepositorioInbox[T],
        manejador: Manejador[T],
        *,
        intervalo_s: float = INTERVALO_S,
        intervalo_max_s: float = INTERVALO_MAX_S,
        reloj: Callable[[], datetime] = _ahora,
        concurrencia: int | None = None,
        visibilidad_s: int | None = None,
        latido_s: float | None = None,
        plazo_parada_s: float = PLAZO_PARADA_S,
        al_fallar: AlFallar | None = None,
    ) -> None:
        if concurrencia is not None and not 1 <= concurrencia <= CONCURRENCIA_MAX:
            raise ValueError(f"concurrencia debe estar entre 1 y {CONCURRENCIA_MAX}")
        if visibilidad_s is not None and visibilidad_s < 2:
            raise ValueError("visibilidad_s debe ser de al menos 2 s")
        if latido_s is not None and visibilidad_s is not None and latido_s >= visibilidad_s:
            raise ValueError("latido_s debe ser menor que visibilidad_s")
        controlado = isinstance(receptor, ReceptorControlado)
        if (concurrencia is not None or visibilidad_s is not None) and not controlado:
            raise TypeError("concurrencia y visibilidad_s piden un ReceptorControlado")
        self._receptor = receptor
        self._repositorio = repositorio
        self._manejador = manejador
        self._intervalo_s = intervalo_s
        self._intervalo_max_s = intervalo_max_s
        self._reloj = reloj
        self._concurrencia = concurrencia
        self._visibilidad_s = visibilidad_s
        self._latido_s = latido_s if latido_s is not None else (visibilidad_s or 0) / 2
        self._plazo_parada_s = plazo_parada_s
        self._al_fallar = al_fallar
        self._controlado = controlado
        self._semaforo = asyncio.Semaphore(concurrencia or 1)
        # Recibidos sin terminar (en curso o esperando turno): la medida de la contrapresión.
        self._en_mano = 0
        self._hueco = asyncio.Event()
        self._parada: asyncio.Event | None = None

    @property
    def en_mano(self) -> int:
        """Mensajes recibidos que aún no terminan (para pruebas y métricas del servicio)."""
        return self._en_mano

    async def ejecutar_una_vez(self) -> int:
        """Recibe un lote y lo procesa (en secuencia, o con `concurrencia` a la vez); devuelve
        cuántos mensajes procesó (nuevos y duplicados). Un fallo de recepción se propaga."""
        mensajes = await self._recibir()
        if not mensajes:
            return 0
        resultados = await asyncio.gather(*(self._atender(mensaje) for mensaje in mensajes))
        return sum(resultados)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        """Repite ciclos hasta `parada`. Sin pausa tras una recepción (la espera larga ya marca el
        ritmo); tras fallos de recepción seguidos, una espera que crece hasta `intervalo_max_s`."""
        self._parada = parada
        if self._concurrencia is None:
            await self._ejecutar_en_secuencia(parada)
        else:
            await self._ejecutar_con_contrapresion(parada)

    async def _ejecutar_en_secuencia(self, parada: asyncio.Event) -> None:
        fallos_seguidos = 0
        while not parada.is_set():
            try:
                await self.ejecutar_una_vez()
            except Exception as error:  # noqa: BLE001 - el siguiente ciclo lo reintenta
                fallos_seguidos += 1
                await self._esperar_tras_fallo(parada, error, fallos_seguidos)
            else:
                fallos_seguidos = 0

    async def _ejecutar_con_contrapresion(self, parada: asyncio.Event) -> None:
        """Recibe solo los huecos libres y procesa sin esperar al lote; con la parada, drena."""
        assert self._concurrencia is not None
        tareas: set[asyncio.Task[bool]] = set()
        fallos_seguidos = 0
        while not parada.is_set():
            if self._en_mano >= self._concurrencia:
                self._hueco.clear()
                if self._en_mano >= self._concurrencia:
                    await _esperar_alguno(parada, self._hueco)
                continue
            try:
                mensajes = await self._recibir_o_parar(parada)
            except Exception as error:  # noqa: BLE001 - el siguiente ciclo lo reintenta
                fallos_seguidos += 1
                await self._esperar_tras_fallo(parada, error, fallos_seguidos)
                continue
            fallos_seguidos = 0
            for mensaje in mensajes:
                tarea = asyncio.create_task(self._atender(mensaje))
                tareas.add(tarea)
                tarea.add_done_callback(tareas.discard)
        await self._drenar(tareas)

    async def _esperar_tras_fallo(
        self, parada: asyncio.Event, error: Exception, fallos_seguidos: int
    ) -> None:
        logger.error(MENSAJE_RECEPCION_FALLIDA, extra={"error_type": describir_error(error)})
        espera = espera_tras_fallos(self._intervalo_s, self._intervalo_max_s, fallos_seguidos - 1)
        await _esperar(parada, espera)

    async def _recibir(self) -> list[MensajeSqs]:
        """Con contrapresión pide solo los huecos libres; los recibidos quedan "en mano"."""
        if self._concurrencia is None:
            mensajes = await self._receptor.recibir()
        else:
            assert isinstance(self._receptor, ReceptorControlado)
            libres = self._concurrencia - self._en_mano
            mensajes = await self._receptor.recibir_hasta(min(libres, LOTE_SQS))
        self._en_mano += len(mensajes)
        return mensajes

    async def _recibir_o_parar(self, parada: asyncio.Event) -> list[MensajeSqs]:
        """La espera larga no detiene la parada: si llega antes, se abandona la recepción. Lo que
        traiga se libera si aún hay tiempo; si no, reaparece tras la visibilidad de la cola."""
        recepcion = asyncio.ensure_future(self._recibir())
        senal = asyncio.ensure_future(parada.wait())
        await asyncio.wait({recepcion, senal}, return_when=asyncio.FIRST_COMPLETED)
        senal.cancel()
        if recepcion.done():
            return recepcion.result()
        recepcion.add_done_callback(self._liberar_tardios)
        return []

    def _liberar_tardios(self, recepcion: "asyncio.Future[list[MensajeSqs]]") -> None:
        if recepcion.cancelled() or recepcion.exception() is not None:
            return
        for mensaje in recepcion.result():
            asyncio.ensure_future(self._liberar_y_soltar(mensaje))

    async def _liberar_y_soltar(self, mensaje: MensajeSqs) -> None:
        try:
            await self._liberar(mensaje)
        finally:
            self._soltar()

    async def _drenar(self, tareas: set["asyncio.Task[bool]"]) -> None:
        """Los que esperan turno se liberan solos (ven la parada); los en curso tienen el plazo."""
        if not tareas:
            return
        _, pendientes = await asyncio.wait(set(tareas), timeout=self._plazo_parada_s)
        for tarea in pendientes:
            tarea.cancel()
        await asyncio.gather(*pendientes, return_exceptions=True)

    def _detenido(self) -> bool:
        return self._parada is not None and self._parada.is_set()

    def _soltar(self) -> None:
        self._en_mano -= 1
        self._hueco.set()

    async def _atender(self, mensaje: MensajeSqs) -> bool:
        """Turno, latido y liberación de un mensaje recibido; True si quedó procesado."""
        en_curso = _EnCurso(mensaje)
        latido = (
            asyncio.create_task(self._latir(mensaje)) if self._visibilidad_s is not None else None
        )
        try:
            async with self._semaforo:
                if self._detenido() and self._controlado:
                    await self._liberar(mensaje)
                    return False
                return await self._procesar(en_curso)
        except asyncio.CancelledError:
            if self._controlado and not en_curso.resuelto:
                await asyncio.shield(self._liberar(mensaje))
            raise
        finally:
            if latido is not None:
                latido.cancel()
            self._soltar()

    async def _latir(self, mensaje: MensajeSqs) -> None:
        assert self._visibilidad_s is not None
        assert isinstance(self._receptor, ReceptorControlado)
        while True:
            await asyncio.sleep(self._latido_s)
            try:
                await self._receptor.extender(mensaje, self._visibilidad_s)
            except Exception as error:  # noqa: BLE001 - el siguiente latido lo reintenta
                logger.warning(
                    MENSAJE_EXTENSION_FALLIDA,
                    extra={"message_id": mensaje.message_id, "error_type": describir_error(error)},
                )

    async def _liberar(self, mensaje: MensajeSqs) -> None:
        assert isinstance(self._receptor, ReceptorControlado)
        try:
            await self._receptor.liberar(mensaje)
        except Exception as error:  # noqa: BLE001 - reaparece tras la visibilidad de la cola
            logger.error(
                MENSAJE_LIBERACION_FALLIDA,
                extra={"message_id": mensaje.message_id, "error_type": describir_error(error)},
            )
        else:
            logger.info(MENSAJE_LIBERADO, extra={"message_id": mensaje.message_id})

    async def _procesar(self, en_curso: _EnCurso) -> bool:
        """Procesa un mensaje y lo mide; True si quedó procesado (y se intentó borrar)."""
        mensaje = en_curso.mensaje
        try:
            evento = leer_sobre(mensaje)
        except SobreInvalido as invalido:
            with operacion(OPERACION) as medicion:
                desenlace = Desenlace(ENVENENADO, reason=invalido.motivo)
                await self._registrar_fallo(mensaje, None, desenlace)
                self._anotar(medicion, mensaje, None, desenlace)
            return False
        token = correlation.establecer(evento.correlation_id) if evento.correlation_id else None
        try:
            with operacion(OPERACION) as medicion:
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
                    en_curso.resuelto = True
                    await self._borrar(mensaje)
                else:
                    await self._registrar_fallo(mensaje, evento, desenlace)
                self._anotar(medicion, mensaje, evento, desenlace)
        finally:
            if token is not None:
                correlation.restablecer(token)
        return desenlace.procesado

    async def _aplicar(self, evento: EventoRecibido) -> Desenlace:
        """Inbox y manejador en una transacción; después, la continuación (modo b)."""
        continuacion: Continuacion | None = None
        try:
            async with self._repositorio.transaccion() as tx:
                nuevo = await tx.registrar(evento.event_id, evento.event_type, self._reloj())
                if nuevo:
                    continuacion = await self._manejador(evento, tx)
        except EventoInvalido:
            return Desenlace(ENVENENADO, reason=MOTIVO_ESQUEMA)
        except Exception as error:  # noqa: BLE001 - el mensaje vuelve tras la visibilidad
            return Desenlace(FALLO_TRANSACCION, error_type=describir_error(error))
        if not nuevo:
            return Desenlace(DUPLICADO)
        if continuacion is not None:
            try:
                await continuacion()
            except Exception as error:  # noqa: BLE001 - la reentrega será un duplicado
                return Desenlace(FALLO_CONTINUACION, error_type=describir_error(error))
        return Desenlace(NUEVO)

    async def _borrar(self, mensaje: MensajeSqs) -> None:
        try:
            await self._receptor.borrar(mensaje)
        except Exception as error:  # noqa: BLE001 - la reentrega será un duplicado
            logger.error(
                MENSAJE_BORRADO_FALLIDO,
                extra={"message_id": mensaje.message_id, "error_type": describir_error(error)},
            )

    async def _registrar_fallo(
        self, mensaje: MensajeSqs, evento: EventoRecibido | None, desenlace: Desenlace
    ) -> None:
        if self._al_fallar is None:
            return
        try:
            await self._al_fallar(mensaje, evento, desenlace)
        except Exception as error:  # noqa: BLE001 - la causa es informativa; el mensaje sigue
            logger.error(
                MENSAJE_REGISTRO_FALLO_FALLIDO,
                extra={"message_id": mensaje.message_id, "error_type": describir_error(error)},
            )

    @staticmethod
    def _anotar(
        medicion: operacion,
        mensaje: MensajeSqs,
        evento: EventoRecibido | None,
        desenlace: Desenlace,
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
        medicion.anotar(**extra)
        medicion.marcar(RESULTADO_OK if desenlace.procesado else RESULTADO_ERROR)
