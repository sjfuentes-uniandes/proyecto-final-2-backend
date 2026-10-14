"""Claves públicas del emisor (JWKS de Cognito) con caché y refresco acotado."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

import httpx
import jwt


class ClaveNoEncontrada(Exception):
    """El emisor no publica una clave con ese `kid`."""


class JwksNoDisponible(Exception):
    """No se pudieron obtener las claves del emisor y no hay copia guardada."""


@dataclass(frozen=True, slots=True)
class _Conjunto:
    claves: dict[str, jwt.PyJWK]
    obtenido_en: float
    intentado_en: float


class ProveedorJwks:
    """Refresca al vencer el TTL o ante un `kid` nuevo (rotación), nunca más de una vez por
    `refresco_min_s`: un `kid` inventado no puede forzar una consulta por solicitud.
    """

    def __init__(
        self,
        cliente: httpx.AsyncClient,
        *,
        ttl_s: int,
        timeout_ms: int,
        refresco_min_s: int,
        reloj: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cliente = cliente
        self._ttl_s = ttl_s
        self._timeout_s = timeout_ms / 1000
        self._refresco_min_s = refresco_min_s
        self._reloj = reloj
        self._cache: dict[str, _Conjunto] = {}
        self._bloqueo = asyncio.Lock()

    async def obtener(self, emisor: str, kid: str) -> jwt.PyJWK:
        conjunto = self._cache.get(emisor)
        if conjunto is None or self._debe_refrescar(conjunto, kid):
            conjunto = await self._refrescar(emisor, conjunto)
        clave = conjunto.claves.get(kid)
        if clave is None:
            raise ClaveNoEncontrada()
        return clave

    def _debe_refrescar(self, conjunto: _Conjunto, kid: str) -> bool:
        ahora = self._reloj()
        if ahora - conjunto.intentado_en < self._refresco_min_s:
            return False
        return ahora - conjunto.obtenido_en >= self._ttl_s or kid not in conjunto.claves

    async def _refrescar(self, emisor: str, anterior: _Conjunto | None) -> _Conjunto:
        async with self._bloqueo:
            actual = self._cache.get(emisor)
            if actual is not None and actual is not anterior:
                return actual  # otra solicitud ya lo refrescó
            ahora = self._reloj()
            try:
                respuesta = await self._cliente.get(
                    f"{emisor}/.well-known/jwks.json", timeout=self._timeout_s
                )
                respuesta.raise_for_status()
                claves = {clave["kid"]: jwt.PyJWK(clave) for clave in respuesta.json()["keys"]}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError) as error:
                if anterior is None:
                    raise JwksNoDisponible() from error
                # Conserva las claves guardadas mientras el emisor no responda.
                conjunto = replace(anterior, intentado_en=ahora)
            else:
                conjunto = _Conjunto(claves=claves, obtenido_en=ahora, intentado_en=ahora)
            self._cache[emisor] = conjunto
            return conjunto
