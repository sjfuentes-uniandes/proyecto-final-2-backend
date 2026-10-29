"""Relay del Transactional Outbox (HU-W16, 16.4; ADR 0013, decisión 8).

Publica en SNS las filas pendientes de la tabla `outbox` que escriben los servicios en la misma
transacción que su efecto (forma de `polizas`: `published_at` nulo, `attempts`, `last_error`).

- Un ciclo (`RelayOutbox.ejecutar_una_vez`) abre una transacción, toma hasta `lote` pendientes con
  `FOR UPDATE SKIP LOCKED` (orden `occurred_at`, `event_id`), publica cada uno, marca
  `published_at` en los publicados o escribe `last_error` en los fallidos, y hace commit. Dos relays
  sobre la misma tabla no toman las mismas filas.
- Garantía: **al menos una vez**. Si el proceso cae entre publicar y el commit, la fila sigue
  pendiente y se republica con el mismo `eventId`; los consumidores deduplican por `eventId`. El
  orden no se garantiza (SNS estándar).
- Un evento que falla no bloquea al resto del lote. `last_error` guarda solo el tipo de la
  excepción y el código de AWS, nunca el mensaje ni el payload.
- Solo un fallo **de la fila** (SNS la rechaza con un 4xx que no es 403, 429, throttling ni un error
  del tópico, o su sobre no se puede armar) suma `attempts`. Un fallo **general** (conexión,
  timeouts, 5xx, 403, 429, throttling, tópico inexistente o su clave KMS no disponible) no la
  consume: una caída o una mala configuración de SNS no agota las filas. Al llegar a `max_intentos`
  la fila deja de reintentarse y queda visible (`published_at` nulo), con un ERROR en el log.
- El sobre se reconstruye desde las columnas, idéntico al de `contracts/events/`, y el mensaje SNS
  lleva el atributo `eventType` (= `type`), del que dependen los filtros de las colas.
- No lee variables de entorno: el servicio que lo cablea pasa la tabla, el tópico y los tiempos.
  boto3 es síncrono: se llama en un hilo, con timeouts y reintentos explícitos.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection, sql

from solventa_common import correlation
from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_OK, registrar

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

TABLA = "outbox"
LOTE = 50
INTERVALO_S = 1.0
INTERVALO_MAX_S = 60.0
# Solo cuentan los fallos de la fila: con un rechazo definitivo, reintentar no cambia el resultado.
MAX_INTENTOS = 5
TIMEOUT_CONEXION_S = 1.0
TIMEOUT_LECTURA_S = 3.0
INTENTOS_SNS = 2
LARGO_MAX_ERROR = 200

ATRIBUTO_TIPO = "eventType"
OPERACION = "outbox.publicar"
MENSAJE_AGOTADO = "outbox: evento sin más reintentos"
MENSAJE_CICLO_FALLIDO = "outbox: el ciclo falló"

# Un código de error de AWS es un identificador (InvalidParameter, Throttling...); otra cosa no se
# guarda, por si trae datos.
_CODIGO_AWS = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Tope del exponente de la espera: evita desbordar el float con muchos ciclos fallidos seguidos.
_MAX_DUPLICACIONES = 16
# Throttling con estado 4xx: la lista de botocore (retries/standard.py, _THROTTLED_ERROR_CODES) más
# los de SNS (`Throttled` es 429; `KMSThrottling`, 400).
_THROTTLING = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestThrottledException",
        "TooManyRequestsException",
        "ProvisionedThroughputExceededException",
        "TransactionInProgressException",
        "RequestLimitExceeded",
        "BandwidthLimitExceeded",
        "LimitExceededException",
        "RequestThrottled",
        "SlowDown",
        "PriorRequestNotComplete",
        "EC2ThrottledException",
        "Throttled",
        "KMSThrottling",
    }
)
_ESTADOS_GENERALES = frozenset({403, 429})
# Errores de `Publish` que describen el tópico y no la fila: el tópico no existe, no hay permiso o
# su clave KMS no está disponible. Fuente: el modelo de SNS de botocore 1.43.110
# (botocore/data/sns/2010-03-31/service-2.json, `errors` de la operación Publish): NotFound 404;
# AuthorizationError, InvalidSecurity y KMSOptInRequired 403; KMSAccessDenied, KMSDisabled,
# KMSInvalidState, KMSNotFound y KMSThrottling 400. EndpointDisabled y
# PlatformApplicationDisabled no entran: solo aplican a `TargetArn` (push móvil), que no se usa.
ERRORES_DEL_TOPICO = frozenset(
    {
        "NotFound",
        "AuthorizationError",
        "InvalidSecurity",
        "KMSAccessDenied",
        "KMSDisabled",
        "KMSInvalidState",
        "KMSNotFound",
        "KMSOptInRequired",
        "KMSThrottling",
    }
)

logger = logging.getLogger("solventa.outbox")


@dataclass(frozen=True)
class EventoPendiente:
    """Una fila pendiente del outbox. `payload` es el JSON tal como está en la columna."""

    event_id: UUID
    aggregate_id: UUID
    event_type: str
    event_version: int
    occurred_at: datetime
    correlation_id: str
    payload: str
    attempts: int


def _instante(valor: datetime) -> str:
    # Como pydantic, que serializa los eventos de los servicios: UTC con "Z".
    return valor.astimezone(UTC).isoformat().replace("+00:00", "Z")


class SobreInvalido(Exception):
    """El sobre no se puede armar desde la fila (el payload no es un objeto JSON): fallo de la fila.

    Excepción marcadora: no lleva el payload.
    """


def sobre_json(evento: EventoPendiente) -> str:
    """El sobre de `contracts/events/` como JSON, con el payload tal cual (sin pasar por float)."""
    try:
        es_objeto = isinstance(json.loads(evento.payload), dict)
    except ValueError:
        es_objeto = False
    if not es_objeto:
        raise SobreInvalido
    cabecera = json.dumps(
        {
            "eventId": str(evento.event_id),
            "aggregateId": str(evento.aggregate_id),
            "type": evento.event_type,
            "version": evento.event_version,
            "occurredAt": _instante(evento.occurred_at),
            "correlationId": evento.correlation_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Se empalma el texto de la columna: decodificarlo convertiría los números en float.
    return f'{cabecera[:-1]},"payload":{evento.payload}}}'


def describir_error(error: BaseException) -> str:
    """`last_error`: el tipo y, si es un error de AWS, su código. Nunca el mensaje."""
    descripcion = type(error).__name__
    respuesta = getattr(error, "response", None)
    if isinstance(respuesta, dict):
        codigo = (respuesta.get("Error") or {}).get("Code")
        if isinstance(codigo, str) and _CODIGO_AWS.fullmatch(codigo):
            descripcion = f"{descripcion}: {codigo}"
    return descripcion[:LARGO_MAX_ERROR]


def es_fallo_de_la_fila(error: BaseException) -> bool:
    """True si reintentar la misma fila no cambia el resultado: suma `attempts`.

    Lo es un sobre inválido o un error de AWS con estado HTTP 4xx (de
    `response["ResponseMetadata"]["HTTPStatusCode"]`, que botocore agrega a toda respuesta), salvo
    403, 429, throttling y los errores del tópico (`ERRORES_DEL_TOPICO`). Todo lo demás
    (conexión, timeouts, 5xx, sin estado) es general.
    """
    if isinstance(error, SobreInvalido):
        return True
    respuesta = getattr(error, "response", None)
    if not isinstance(respuesta, dict):
        return False
    estado = (respuesta.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    codigo = (respuesta.get("Error") or {}).get("Code")
    return (
        isinstance(estado, int)
        and 400 <= estado < 500
        and estado not in _ESTADOS_GENERALES
        and codigo not in _THROTTLING
        and codigo not in ERRORES_DEL_TOPICO
    )


def espera_tras_fallos(intervalo_s: float, maximo_s: float, fallos: int) -> float:
    """Espera entre ciclos: se duplica con cada ciclo fallido seguido, hasta `maximo_s`."""
    return float(min(intervalo_s * 2 ** min(fallos, _MAX_DUPLICACIONES), maximo_s))


# --- Puertos -----------------------------------------------------------------------------------
class TransaccionOutbox(Protocol):
    async def pendientes(self, limite: int, max_intentos: int) -> list[EventoPendiente]: ...

    async def marcar_publicados(self, event_ids: Sequence[UUID], cuando: datetime) -> None: ...

    async def registrar_fallo(self, event_id: UUID, error: str, *, definitivo: bool) -> None:
        """Escribe `last_error`; suma `attempts` solo si el fallo es de la fila."""
        ...


class RepositorioOutbox(Protocol):
    def transaccion(self) -> AbstractAsyncContextManager[TransaccionOutbox]: ...


class Publicador(Protocol):
    async def publicar(self, evento: EventoPendiente) -> None: ...


# --- Adaptador PostgreSQL ------------------------------------------------------------------------
_PENDIENTES = sql.SQL(
    "SELECT event_id, aggregate_id, event_type, event_version, occurred_at, correlation_id, "
    "payload::text, attempts FROM {tabla} "
    "WHERE published_at IS NULL AND attempts < %s "
    "ORDER BY occurred_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED"
)
_MARCAR = sql.SQL("UPDATE {tabla} SET published_at = %s WHERE event_id = ANY(%s)")
_FALLO = sql.SQL("UPDATE {tabla} SET attempts = attempts + %s, last_error = %s WHERE event_id = %s")


@dataclass(frozen=True)
class TransaccionOutboxPg:
    conexion: AsyncConnection[Any]
    tabla: sql.Identifier

    async def pendientes(self, limite: int, max_intentos: int) -> list[EventoPendiente]:
        cursor = await self.conexion.execute(
            _PENDIENTES.format(tabla=self.tabla), (max_intentos, limite)
        )
        return [EventoPendiente(*fila) for fila in await cursor.fetchall()]

    async def marcar_publicados(self, event_ids: Sequence[UUID], cuando: datetime) -> None:
        await self.conexion.execute(_MARCAR.format(tabla=self.tabla), (cuando, list(event_ids)))

    async def registrar_fallo(self, event_id: UUID, error: str, *, definitivo: bool) -> None:
        await self.conexion.execute(
            _FALLO.format(tabla=self.tabla), (int(definitivo), error, event_id)
        )


class OutboxPg:
    """Repositorio de pendientes sobre el pool del servicio; una transacción por ciclo."""

    def __init__(self, pool: "AsyncConnectionPool[Any]", tabla: str = TABLA) -> None:
        self._pool = pool
        self._tabla = sql.Identifier(tabla)

    @asynccontextmanager
    async def transaccion(self) -> AsyncIterator[TransaccionOutboxPg]:
        async with self._pool.connection() as conexion, conexion.transaction():
            yield TransaccionOutboxPg(conexion, self._tabla)


# --- Adaptador SNS --------------------------------------------------------------------------------
def cliente_sns(
    region: str,
    *,
    endpoint_url: str | None = None,
    timeout_conexion_s: float = TIMEOUT_CONEXION_S,
    timeout_lectura_s: float = TIMEOUT_LECTURA_S,
    intentos: int = INTENTOS_SNS,
) -> Any:
    """Cliente SNS de boto3 con timeouts y reintentos explicitos (`intentos` incluye el primero)."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "sns",
        region_name=region,
        endpoint_url=endpoint_url,
        config=Config(
            connect_timeout=timeout_conexion_s,
            read_timeout=timeout_lectura_s,
            retries={"total_max_attempts": intentos, "mode": "standard"},
        ),
    )


class PublicadorSns:
    """Publica el sobre en el tópico con el atributo `eventType`, en un hilo (boto3 es síncrono)."""

    def __init__(self, cliente: Any, topic_arn: str) -> None:
        self._cliente = cliente
        self._topic_arn = topic_arn

    async def publicar(self, evento: EventoPendiente) -> None:
        await asyncio.to_thread(
            self._cliente.publish,
            TopicArn=self._topic_arn,
            Message=sobre_json(evento),
            MessageAttributes={
                ATRIBUTO_TIPO: {"DataType": "String", "StringValue": evento.event_type}
            },
        )


# --- Relay -----------------------------------------------------------------------------------------
def _ahora() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Ciclo:
    leidos: int
    publicados: int
    fallidos: int


@dataclass(frozen=True)
class _Fallo:
    descripcion: str
    definitivo: bool
    attempts: int


class RelayOutbox:
    """Publica los pendientes del outbox: un ciclo (`ejecutar_una_vez`) o en bucle (`ejecutar`)."""

    def __init__(
        self,
        repositorio: RepositorioOutbox,
        publicador: Publicador,
        *,
        lote: int = LOTE,
        max_intentos: int = MAX_INTENTOS,
        intervalo_s: float = INTERVALO_S,
        intervalo_max_s: float = INTERVALO_MAX_S,
        reloj: Callable[[], datetime] = _ahora,
    ) -> None:
        if lote < 1 or max_intentos < 1:
            raise ValueError("lote y max_intentos deben ser positivos")
        self._repositorio = repositorio
        self._publicador = publicador
        self._lote = lote
        self._max_intentos = max_intentos
        self._intervalo_s = intervalo_s
        self._intervalo_max_s = intervalo_max_s
        self._reloj = reloj

    async def ejecutar_una_vez(self) -> int:
        """Un ciclo en una transacción; devuelve cuántos eventos publicó."""
        return (await self._ciclo()).publicados

    async def ejecutar(self, parada: asyncio.Event) -> None:
        """Repite ciclos hasta `parada`. Sin esperar si el lote salió lleno y sin fallos; con
        espera creciente si un ciclo no publicó nada por fallos. Un ciclo fallido no lo detiene.
        """
        fallos_seguidos = 0
        while not parada.is_set():
            try:
                ciclo = await self._ciclo()
            except Exception as error:  # noqa: BLE001 - el siguiente ciclo lo reintenta
                logger.error(MENSAJE_CICLO_FALLIDO, extra={"error_type": type(error).__name__})
                ciclo = _Ciclo(leidos=0, publicados=0, fallidos=1)
            if ciclo.leidos == self._lote and ciclo.fallidos == 0:
                fallos_seguidos = 0
                continue
            fallos_seguidos = fallos_seguidos + 1 if ciclo.fallidos and not ciclo.publicados else 0
            espera = espera_tras_fallos(self._intervalo_s, self._intervalo_max_s, fallos_seguidos)
            try:
                await asyncio.wait_for(parada.wait(), espera)
            except TimeoutError:
                pass

    async def _ciclo(self) -> _Ciclo:
        async with self._repositorio.transaccion() as tx:
            pendientes = await tx.pendientes(self._lote, self._max_intentos)
            publicados: list[UUID] = []
            for evento in pendientes:
                fallo = await self._publicar(evento)
                if fallo is None:
                    publicados.append(evento.event_id)
                    continue
                await tx.registrar_fallo(
                    evento.event_id, fallo.descripcion, definitivo=fallo.definitivo
                )
                if fallo.definitivo and fallo.attempts >= self._max_intentos:
                    logger.error(
                        MENSAJE_AGOTADO,
                        extra={
                            "event_id": str(evento.event_id),
                            "event_type": evento.event_type,
                            "attempts": fallo.attempts,
                            "max_intentos": self._max_intentos,
                        },
                    )
            if publicados:
                await tx.marcar_publicados(publicados, self._reloj())
        return _Ciclo(len(pendientes), len(publicados), len(pendientes) - len(publicados))

    async def _publicar(self, evento: EventoPendiente) -> _Fallo | None:
        """Publica un evento y lo mide; devuelve el fallo, o None si salió."""
        token = correlation.establecer(evento.correlation_id)
        inicio = time.perf_counter()
        fallo: _Fallo | None = None
        try:
            await self._publicador.publicar(evento)
        except Exception as excepcion:  # noqa: BLE001 - un evento fallido no frena el lote
            definitivo = es_fallo_de_la_fila(excepcion)
            fallo = _Fallo(
                descripcion=describir_error(excepcion),
                definitivo=definitivo,
                attempts=evento.attempts + int(definitivo),
            )
        try:
            registrar(
                OPERACION,
                (time.perf_counter() - inicio) * 1000,
                RESULTADO_OK if fallo is None else RESULTADO_ERROR,
                event_id=str(evento.event_id),
                event_type=evento.event_type,
                attempts=evento.attempts if fallo is None else fallo.attempts,
                **(
                    {}
                    if fallo is None
                    else {
                        "error_type": fallo.descripcion,
                        "failure_scope": "fila" if fallo.definitivo else "general",
                    }
                ),
            )
        finally:
            correlation.restablecer(token)
        return fallo
