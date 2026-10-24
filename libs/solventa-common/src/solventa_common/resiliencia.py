"""Protección de llamadas a terceros (regla 7, DT-10 y DT-21).

- `timeout_efectivo_ms`: min(configurado, corte duro de 700, deadline - margen).
- `Proteccion`: contrato que usan `adaptador-datos` y `cotizacion`. `SinProteccion` solo aplica
  el timeout; `ProteccionDependencia` agrega el reintento acotado, el circuit breaker por clave
  (fuente) y el bulkhead por grupo (aliado).
- HU-W05: cada llamada anota en el span actual `solventa.timeout_ms`, `solventa.restante_ms` y
  `solventa.presupuesto_ms` (si hay un recorrido con presupuesto), y un timeout deja el WARNING
  "dependencia agotó su tiempo" con proveedor (grupo), operación (clave), timeout y restante. El
  correlation_id lo agrega el formateador de logs.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal, Protocol, TypeVar

import httpx
from opentelemetry import trace

from solventa_common import presupuesto

CORTE_DURO_MS = 700

logger = logging.getLogger("solventa.resiliencia")

T = TypeVar("T")
Motivo = Literal["ERROR", "CIRCUITO_ABIERTO", "BULKHEAD"]
EstadoCircuito = Literal["CERRADO", "ABIERTO", "SEMIABIERTO"]


class TiempoAgotado(Exception):
    """La dependencia no respondió dentro del timeout efectivo.

    Excepción marcadora: se distingue por tipo y no necesita datos adicionales.
    """


class DependenciaNoDisponible(Exception):
    """La dependencia falló (error, 5xx) o la protección no dejó llamarla."""

    def __init__(self, motivo: Motivo, circuito: EstadoCircuito) -> None:
        super().__init__(motivo)
        self.motivo: Motivo = motivo
        self.circuito: EstadoCircuito = circuito


def timeout_efectivo_ms(
    configurado_ms: int,
    deadline_ms: int | None,
    margen_ms: int = 20,
    corte_duro_ms: int = CORTE_DURO_MS,
) -> int:
    """min(configurado, corte duro, deadline - margen), nunca menor que 1."""
    limites = [configurado_ms, corte_duro_ms]
    if deadline_ms is not None:
        limites.append(deadline_ms - margen_ms)
    return max(1, min(limites))


def anotar_llamada(timeout_ms: int, deadline_ms: int | None) -> None:
    """Atributos de presupuesto en el span actual (el de la operación de la dependencia)."""
    span = trace.get_current_span()
    span.set_attribute(presupuesto.ATRIBUTO_TIMEOUT, timeout_ms)
    if deadline_ms is not None:
        span.set_attribute(presupuesto.ATRIBUTO_RESTANTE, deadline_ms)
    recorrido = presupuesto.actual()
    if recorrido is not None:
        span.set_attribute(presupuesto.ATRIBUTO_PRESUPUESTO, recorrido.total_ms)


def registrar_tiempo_agotado(
    proveedor: str, operacion: str, timeout_ms: int, restante_ms: int | None
) -> None:
    """AC1 (HU-W05): el timeout queda en el log con proveedor, operación y correlación."""
    logger.warning(
        "dependencia agotó su tiempo",
        extra={
            "proveedor": proveedor,
            "operacion": operacion,
            "timeout_ms": timeout_ms,
            "restante_ms": restante_ms,
        },
    )


def _restante_tras(inicio_s: float, deadline_ms: int | None) -> int | None:
    """Lo que queda del recorrido al cortar: el presupuesto en curso o el deadline recibido."""
    recorrido = presupuesto.restante_ms()
    if recorrido is not None:
        return recorrido
    if deadline_ms is None:
        return None
    return max(0, deadline_ms - int((time.monotonic() - inicio_s) * 1000))


class Proteccion(Protocol):
    async def ejecutar(
        self,
        clave: str,
        grupo: str,
        llamada: Callable[[float], Awaitable[T]],
        configurado_ms: int,
        deadline_ms: int | None,
    ) -> T:
        """Ejecuta `llamada(timeout_s)` protegida; lanza TiempoAgotado o DependenciaNoDisponible."""
        ...


class SinProteccion:
    """Solo aplica el timeout efectivo y traduce los errores de HTTPX.

    El corte duro es de 700 ms (regla 7). Una operación que por naturaleza tarda más, como la
    verificación biométrica con el proveedor KYC, declara el suyo con `corte_duro_ms`.
    """

    def __init__(self, corte_duro_ms: int = CORTE_DURO_MS) -> None:
        self._corte_duro_ms = corte_duro_ms

    async def ejecutar(
        self,
        clave: str,
        grupo: str,
        llamada: Callable[[float], Awaitable[T]],
        configurado_ms: int,
        deadline_ms: int | None,
    ) -> T:
        timeout_ms = timeout_efectivo_ms(
            configurado_ms, deadline_ms, corte_duro_ms=self._corte_duro_ms
        )
        timeout_s = timeout_ms / 1000
        anotar_llamada(timeout_ms, deadline_ms)
        inicio = time.monotonic()
        try:
            return await asyncio.wait_for(llamada(timeout_s), timeout=timeout_s)
        except (httpx.TimeoutException, TimeoutError) as error:
            registrar_tiempo_agotado(grupo, clave, timeout_ms, _restante_tras(inicio, deadline_ms))
            raise TiempoAgotado(clave) from error
        except httpx.HTTPError as error:
            # Incluye HTTPStatusError (5xx con raise_for_status) y los errores de conexión.
            raise DependenciaNoDisponible("ERROR", "CERRADO") from error


Reloj = Callable[[], float]


class CircuitBreaker:
    """Circuito CERRADO → ABIERTO → SEMIABIERTO con una sola prueba en curso.

    - CERRADO: siempre permite; `fallos_consecutivos` fallos seguidos lo abren.
    - ABIERTO: no permite hasta que pasan `apertura_s`; entonces pasa a SEMIABIERTO y permite
      una prueba.
    - SEMIABIERTO: no permite mientras la prueba está en curso. Éxito → CERRADO; fallo →
      ABIERTO con un instante nuevo.
    """

    def __init__(
        self, fallos_consecutivos: int = 5, apertura_s: float = 30, reloj: Reloj = time.monotonic
    ) -> None:
        self._umbral = fallos_consecutivos
        self._apertura_s = apertura_s
        self._reloj = reloj
        self._estado: EstadoCircuito = "CERRADO"
        self._fallos = 0
        self._abierto_en = 0.0
        self._prueba_en_curso = False

    @property
    def estado(self) -> EstadoCircuito:
        return self._estado

    def permitir(self) -> bool:
        if self._estado == "CERRADO":
            return True
        if self._estado == "ABIERTO":
            if self._reloj() - self._abierto_en < self._apertura_s:
                return False
            self._estado = "SEMIABIERTO"
            self._prueba_en_curso = True
            return True
        return not self._prueba_en_curso

    def registrar_exito(self) -> None:
        self._estado = "CERRADO"
        self._fallos = 0
        self._prueba_en_curso = False

    def registrar_fallo(self) -> None:
        if self._estado == "SEMIABIERTO":
            self._abrir()
            return
        self._fallos += 1
        if self._fallos >= self._umbral:
            self._abrir()

    def liberar_prueba(self) -> None:
        """La prueba terminó sin éxito ni fallo (p. ej., respuesta inválida): se permite otra."""
        self._prueba_en_curso = False

    def _abrir(self) -> None:
        self._estado = "ABIERTO"
        self._abierto_en = self._reloj()
        self._prueba_en_curso = False


class BulkheadLleno(Exception):
    """El grupo ya tiene `maximo` llamadas en curso. Excepción marcadora."""


class Bulkhead:
    """Límite de llamadas concurrentes por grupo; nunca espera un cupo."""

    def __init__(self, maximo: int) -> None:
        self._semaforo = asyncio.Semaphore(maximo)

    @asynccontextmanager
    async def cupo(self) -> AsyncIterator[None]:
        if self._semaforo.locked():
            raise BulkheadLleno()
        async with self._semaforo:
            yield


def es_conexion_o_5xx(error: BaseException) -> bool:
    """Errores reintentables: conexión o 5xx. Nunca un timeout."""
    if isinstance(error, httpx.ConnectError | httpx.RemoteProtocolError):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500


async def con_reintento[R](
    fn: Callable[[], Awaitable[R]],
    *,
    intentos: int,
    espera_s: float,
    reintentable: Callable[[BaseException], bool],
    presupuesto_restante_s: Callable[[], float],
    minimo_s: float,
) -> R:
    """Ejecuta `fn` y la repite hasta `intentos` veces si el error es reintentable y queda
    presupuesto (restante − espera ≥ mínimo). Si no, relanza el error."""
    restantes = intentos
    while True:
        try:
            return await fn()
        except Exception as error:
            if (
                restantes <= 0
                or not reintentable(error)
                or presupuesto_restante_s() - espera_s < minimo_s
            ):
                raise
            restantes -= 1
            await asyncio.sleep(espera_s)


class ProteccionDependencia:
    """Implementa `Proteccion`: timeout efectivo, reintento acotado (1 intento, 25 ms, solo
    conexión o 5xx y si quedan ≥ 100 ms), circuito por `clave` y bulkhead por `grupo` (DT-21).

    Un timeout o un error HTTPX cuentan como fallo del circuito; el bulkhead lleno y cualquier
    otra excepción (p. ej., una respuesta inválida) no cuentan.
    """

    def __init__(
        self,
        fallos_consecutivos: int,
        apertura_s: float,
        bulkhead_max: int,
        reloj: Reloj = time.monotonic,
    ) -> None:
        self._fallos_consecutivos = fallos_consecutivos
        self._apertura_s = apertura_s
        self._bulkhead_max = bulkhead_max
        self._reloj = reloj
        self.circuitos: dict[str, CircuitBreaker] = {}
        self.bulkheads: dict[str, Bulkhead] = {}

    def _circuito(self, clave: str) -> CircuitBreaker:
        if clave not in self.circuitos:
            self.circuitos[clave] = CircuitBreaker(
                self._fallos_consecutivos, self._apertura_s, self._reloj
            )
        return self.circuitos[clave]

    def _bulkhead(self, grupo: str) -> Bulkhead:
        if grupo not in self.bulkheads:
            self.bulkheads[grupo] = Bulkhead(self._bulkhead_max)
        return self.bulkheads[grupo]

    async def ejecutar(
        self,
        clave: str,
        grupo: str,
        llamada: Callable[[float], Awaitable[T]],
        configurado_ms: int,
        deadline_ms: int | None,
    ) -> T:
        cb = self._circuito(clave)
        if not cb.permitir():
            raise DependenciaNoDisponible("CIRCUITO_ABIERTO", cb.estado)
        timeout_ms = timeout_efectivo_ms(configurado_ms, deadline_ms)
        anotar_llamada(timeout_ms, deadline_ms)
        inicio = time.monotonic()
        fin = self._reloj() + timeout_ms / 1000

        def restante() -> float:
            return max(0.0, fin - self._reloj())

        registrado = False
        try:
            async with self._bulkhead(grupo).cupo():
                resultado = await con_reintento(
                    lambda: asyncio.wait_for(llamada(restante()), restante()),
                    intentos=1,
                    espera_s=0.025,
                    reintentable=es_conexion_o_5xx,
                    presupuesto_restante_s=restante,
                    minimo_s=0.1,
                )
        except BulkheadLleno as error:
            raise DependenciaNoDisponible("BULKHEAD", cb.estado) from error
        except (httpx.TimeoutException, TimeoutError) as error:
            cb.registrar_fallo()
            registrado = True
            registrar_tiempo_agotado(grupo, clave, timeout_ms, _restante_tras(inicio, deadline_ms))
            raise TiempoAgotado(clave) from error
        except httpx.HTTPError as error:
            cb.registrar_fallo()
            registrado = True
            raise DependenciaNoDisponible("ERROR", cb.estado) from error
        else:
            cb.registrar_exito()
            registrado = True
            return resultado
        finally:
            if not registrado:
                cb.liberar_prueba()


async def acotar[R](
    fn: Callable[[], Awaitable[R]],
    *,
    operacion: str,
    proveedor: str,
    configurado_ms: int,
    deadline_ms: int | None,
) -> R:
    """Trabajo propio de quien recibe X-Deadline-Ms (HU-W05): corta en
    timeout_efectivo_ms(configurado, deadline) y lanza TiempoAgotado, para no seguir trabajando
    cuando quien llama ya dejó de esperar. La ruta lo traduce a 504 TIEMPO_AGOTADO.

    Cancela la corrutina; un trabajo en `asyncio.to_thread` termina en su hilo aunque la
    respuesta ya haya salido.
    """
    return await SinProteccion().ejecutar(
        operacion, proveedor, lambda _timeout_s: fn(), configurado_ms, deadline_ms
    )
