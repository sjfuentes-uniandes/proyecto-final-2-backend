"""Protección de llamadas a terceros (regla 7, DT-10 y DT-21).

Sprint 1, HU-W10: solo la estructura y `SinProteccion` (timeout efectivo).
HU-W12 agrega `ProteccionDependencia` (reintento, circuit breaker y bulkhead).
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
    """La dependencia no respondió dentro del timeout efectivo."""


class DependenciaNoDisponible(Exception):
    """La dependencia falló (error, 5xx) o la protección no dejó llamarla."""

    def __init__(self, motivo: Motivo, circuito: EstadoCircuito) -> None:
        super().__init__(motivo)
        self.motivo: Motivo = motivo
        self.circuito: EstadoCircuito = circuito


def timeout_efectivo_ms(configurado_ms: int, deadline_ms: int | None, margen_ms: int = 20) -> int:
    """min(configurado, corte duro, deadline - margen), nunca menor que 1."""
    limites = [configurado_ms, CORTE_DURO_MS]
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
    """Solo aplica el timeout efectivo y traduce los errores de HTTPX."""

    async def ejecutar(
        self,
        clave: str,
        grupo: str,
        llamada: Callable[[float], Awaitable[T]],
        configurado_ms: int,
        deadline_ms: int | None,
    ) -> T:
        timeout_s = timeout_efectivo_ms(configurado_ms, deadline_ms) / 1000
        try:
            return await asyncio.wait_for(llamada(timeout_s), timeout=timeout_s)
        except (httpx.TimeoutException, TimeoutError) as error:
            raise TiempoAgotado(clave) from error
        except httpx.HTTPError as error:
            # Incluye HTTPStatusError (5xx con raise_for_status) y los errores de conexión.
            raise DependenciaNoDisponible("ERROR", "CERRADO") from error
