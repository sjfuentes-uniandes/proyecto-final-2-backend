"""PuertoProveedorKyc sobre HTTPX."""

from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr

from adaptador_identidad.domain.verificaciones.modelos import (
    RespuestaInvalida,
    ResultadoVerificacion,
    Verificacion,
)


class KycHttp:
    """Cliente del proveedor KYC simulado. Nunca registra la selfie ni la credencial."""

    def __init__(self, cliente: httpx.AsyncClient, base_url: str, api_key: SecretStr) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def verificar(
        self,
        cliente_id: UUID,
        selfie: str,
        prueba_de_vida: dict[str, Any],
        timeout_s: float,
    ) -> Verificacion:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/verificaciones",
            json={"clienteId": str(cliente_id), "selfie": selfie, "pruebaDeVida": prueba_de_vida},
            headers={"X-Api-Key": self._api_key.get_secret_value()},
            timeout=timeout_s,
        )
        if 400 <= respuesta.status_code < 500:
            raise RespuestaInvalida("HTTP_4XX")
        respuesta.raise_for_status()
        try:
            cuerpo = respuesta.json()
            return Verificacion(
                resultado=ResultadoVerificacion(cuerpo["resultado"]), motivo=cuerpo.get("motivo")
            )
        except (ValueError, KeyError) as error:
            raise RespuestaInvalida("JSON") from error
