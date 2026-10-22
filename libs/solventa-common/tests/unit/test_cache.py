"""Cache-Aside sobre Valkey: nunca lanza, mide cada operación y abre el circuito ante fallas."""

import asyncio
from typing import Any

import pytest
from solventa_common.cache import CacheValkey, SinCache, crear_cache
from solventa_common.resiliencia import CircuitBreaker

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ClienteFalso:
    def __init__(self, error: BaseException | None = None, demora_s: float = 0) -> None:
        self.valores: dict[str, bytes] = {}
        self.ttl: dict[str, int | None] = {}
        self.error = error
        self.demora_s = demora_s
        self.llamadas = 0
        self.cerrado = False

    async def _quizas_fallar(self) -> None:
        self.llamadas += 1
        if self.demora_s:
            await asyncio.sleep(self.demora_s)
        if self.error:
            raise self.error

    async def get(self, name: str) -> Any:
        await self._quizas_fallar()
        return self.valores.get(name)

    async def set(self, name: str, value: bytes, ex: int | None = None) -> Any:
        await self._quizas_fallar()
        self.valores[name] = value
        self.ttl[name] = ex
        return True

    async def delete(self, *names: str) -> Any:
        await self._quizas_fallar()
        for name in names:
            self.valores.pop(name, None)
        return len(names)

    async def aclose(self) -> None:
        if self.error:
            raise self.error
        self.cerrado = True


async def test_escribir_leer_y_borrar() -> None:
    cliente = ClienteFalso()
    cache = CacheValkey(cliente, timeout_ms=50)
    assert await cache.leer("k") is None
    await cache.escribir("k", b"v", 60)
    assert await cache.leer("k") == b"v"
    assert cliente.ttl["k"] == 60
    await cache.borrar("k")
    await cache.borrar()  # sin claves no llama
    assert await cache.leer("k") is None
    await cache.cerrar()
    assert cliente.cerrado


async def test_una_falla_es_un_fallo_de_cache_y_no_lanza() -> None:
    cache = CacheValkey(ClienteFalso(error=ConnectionError("sin red")), timeout_ms=50)
    assert await cache.leer("k") is None
    await cache.escribir("k", b"v", 60)
    await cache.borrar("k")
    await cache.cerrar()


async def test_timeout_es_un_fallo_de_cache() -> None:
    cache = CacheValkey(ClienteFalso(demora_s=0.2), timeout_ms=10)
    assert await cache.leer("k") is None


async def test_el_circuito_abierto_omite_la_cache() -> None:
    cliente = ClienteFalso(error=ConnectionError("sin red"))
    cache = CacheValkey(cliente, 50, CircuitBreaker(fallos_consecutivos=2, apertura_s=60))
    for _ in range(5):
        assert await cache.leer("k") is None
    assert cliente.llamadas == 2


async def test_un_valor_que_no_es_bytes_se_ignora() -> None:
    cliente = ClienteFalso()
    cliente.valores["k"] = "texto"  # type: ignore[assignment]
    assert await CacheValkey(cliente, 50).leer("k") is None


async def test_sin_cache_no_guarda_nada() -> None:
    cache = SinCache()
    await cache.escribir("k", b"v", 60)
    assert await cache.leer("k") is None
    await cache.borrar("k")
    await cache.cerrar()


def test_crear_cache_segun_la_url() -> None:
    assert isinstance(crear_cache(None, 50), SinCache)
    assert isinstance(crear_cache("", 50), SinCache)
    # No conecta al crearse: la conexión se abre en la primera operación.
    assert isinstance(crear_cache("rediss://cache.example:6379", 50), CacheValkey)
