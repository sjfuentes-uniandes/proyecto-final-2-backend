"""Protección de llamadas a terceros (regla 7, DT-10 y DT-21).

- `timeout_efectivo_ms`: min(configurado, corte duro de 700, deadline - margen).
- `Proteccion`: contrato que usa `adaptador-datos`. `SinProteccion` solo aplica el timeout;
  `ProteccionDependencia` agrega el reintento acotado, el circuit breaker por clave (fuente) y
  el bulkhead por grupo (aliado).
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal, Protocol, TypeVar

import httpx

CORTE_DURO_MS = 700

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
        timeout_s = (
            timeout_efectivo_ms(configurado_ms, deadline_ms, corte_duro_ms=self._corte_duro_ms)
            / 1000
        )
        try:
            return await asyncio.wait_for(llamada(timeout_s), timeout=timeout_s)
        except (httpx.TimeoutException, TimeoutError) as error:
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
        fin = self._reloj() + timeout_efectivo_ms(configurado_ms, deadline_ms) / 1000

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
