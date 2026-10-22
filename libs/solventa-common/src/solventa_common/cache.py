"""Cache-Aside sobre Valkey (ElastiCache de HU-S02, `CACHE_URL`).

La caché es reemplazable (infra/platform/cache.tf): una falla (sin conexión, timeout o circuito
abierto) se trata como un fallo de caché y el llamador va a su fuente de verdad. Ninguna operación
lanza. Cada una se mide con `operacion("cache.leer" | "cache.escribir" | "cache.borrar")`; la
lectura marca `ok` si encuentra la clave y `vacio` si no.

`redis` se importa solo al crear el cliente: los servicios sin caché no lo necesitan.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from solventa_common.resiliencia import CircuitBreaker
from solventa_common.telemetry import RESULTADO_ERROR, RESULTADO_TIMEOUT, operacion

RESULTADO_VACIO = "vacio"

T = TypeVar("T")
logger = logging.getLogger("solventa.cache")


class Cache(Protocol):
    async def leer(self, clave: str) -> bytes | None: ...

    async def escribir(self, clave: str, valor: bytes, ttl_s: int) -> None: ...

    async def borrar(self, *claves: str) -> None: ...

    async def cerrar(self) -> None: ...


class SinCache:
    """Sin `CACHE_URL`: nada se guarda y toda lectura es un fallo de caché."""

    async def leer(self, clave: str) -> bytes | None:
        return None

    async def escribir(self, clave: str, valor: bytes, ttl_s: int) -> None:
        return None

    async def borrar(self, *claves: str) -> None:
        return None

    async def cerrar(self) -> None:
        return None


class ClienteValkey(Protocol):
    """Lo que se usa de `redis.asyncio.Redis`."""

    def get(self, name: str) -> Awaitable[Any]: ...

    def set(self, name: str, value: bytes, ex: int | None = None) -> Awaitable[Any]: ...

    def delete(self, *names: str) -> Awaitable[Any]: ...

    async def aclose(self) -> None: ...


class CacheValkey:
    """Cada operación tiene `timeout_ms`; `fallos_consecutivos` fallas abren el circuito y la
    caché se omite durante `apertura_s`, sin esperar timeouts."""

    def __init__(
        self,
        cliente: ClienteValkey,
        timeout_ms: int,
        circuito: CircuitBreaker | None = None,
    ) -> None:
        self._cliente = cliente
        self._timeout_s = timeout_ms / 1000
        self._circuito = circuito or CircuitBreaker(fallos_consecutivos=3, apertura_s=30)

    @classmethod
    def desde_url(cls, url: str, timeout_ms: int) -> "CacheValkey":
        from redis.asyncio import Redis

        segundos = timeout_ms / 1000
        cliente = Redis.from_url(url, socket_timeout=segundos, socket_connect_timeout=segundos)
        return cls(cliente, timeout_ms)

    async def leer(self, clave: str) -> bytes | None:
        valor = await self._ejecutar("cache.leer", lambda: self._cliente.get(clave))
        return valor if isinstance(valor, bytes) else None

    async def escribir(self, clave: str, valor: bytes, ttl_s: int) -> None:
        await self._ejecutar("cache.escribir", lambda: self._cliente.set(clave, valor, ex=ttl_s))

    async def borrar(self, *claves: str) -> None:
        if claves:
            await self._ejecutar("cache.borrar", lambda: self._cliente.delete(*claves))

    async def cerrar(self) -> None:
        try:
            await self._cliente.aclose()
        except Exception:  # noqa: BLE001 - cerrar la caché nunca impide apagar el servicio
            logger.warning("no se pudo cerrar la conexión de la caché")

    async def _ejecutar(self, nombre: str, llamada: Callable[[], Awaitable[T]]) -> T | None:
        if not self._circuito.permitir():
            return None
        with operacion(nombre) as op:
            try:
                valor = await asyncio.wait_for(llamada(), self._timeout_s)
            except TimeoutError:
                self._circuito.registrar_fallo()
                op.marcar(RESULTADO_TIMEOUT)
                return None
            except Exception as error:  # noqa: BLE001 - la caché es reemplazable (ver arriba)
                self._circuito.registrar_fallo()
                op.marcar(RESULTADO_ERROR)
                logger.warning("falla de la caché", extra={"error_type": type(error).__name__})
                return None
            self._circuito.registrar_exito()
            if nombre == "cache.leer" and valor is None:
                op.marcar(RESULTADO_VACIO)
            return valor


def crear_cache(url: str | None, timeout_ms: int) -> Cache:
    """`CacheValkey` si hay `CACHE_URL`; si no, `SinCache`."""
    return CacheValkey.desde_url(url, timeout_ms) if url else SinCache()
