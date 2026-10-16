"""Protección de llamadas a terceros: timeout efectivo y errores de dependencia.

Aquí solo está lo que ya usa `adaptador-datos`: las excepciones, el contrato `Proteccion` y
`SinProteccion`, que únicamente aplica el timeout. El reintento, el circuit breaker y el
bulkhead (`ProteccionDependencia`) los agrega la historia de consulta en paralelo.
"""

import asyncio
from collections.abc import Awaitable, Callable
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
