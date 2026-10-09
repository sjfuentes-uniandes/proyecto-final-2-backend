"""PuertoProveedor sobre HTTPX, parametrizado por el catálogo de fuentes (03, W10-P03)."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr

from adaptador_datos.domain.consultas.catalogo_fuentes import CATALOGO
from adaptador_datos.domain.consultas.modelos import Aliado, Fuente, RespuestaInvalida


class ProveedorHttp:
    """Cliente de un aliado simulado. Nunca registra el cuerpo ni la credencial."""

    aliado: Aliado

    def __init__(self, cliente: httpx.AsyncClient, base_url: str, api_key: SecretStr) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def obtener(
        self, fuente: Fuente, cliente_id: UUID, campos_proveedor: tuple[str, ...], timeout_s: float
    ) -> Mapping[str, Any]:
        definicion = CATALOGO[fuente]
        if definicion.aliado is not self.aliado:
            raise ValueError(f"{fuente} no pertenece a {self.aliado}")
        url = self._base_url + definicion.ruta.format(cliente_id=cliente_id)
        params = {**definicion.extras, definicion.parametro: ",".join(campos_proveedor)}
        respuesta = await self._cliente.get(
            url,
            params=params,
            headers={"X-Api-Key": self._api_key.get_secret_value()},
            timeout=timeout_s,
        )
        if 400 <= respuesta.status_code < 500:
            raise RespuestaInvalida("HTTP_4XX")
        respuesta.raise_for_status()
        try:
            cuerpo: Mapping[str, Any] = respuesta.json()
        except ValueError as error:
            raise RespuestaInvalida("JSON") from error
        return cuerpo
