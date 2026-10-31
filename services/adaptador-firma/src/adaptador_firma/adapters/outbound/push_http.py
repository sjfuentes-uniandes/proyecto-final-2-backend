"""PuertoProveedorPush sobre HTTPX, hacia el aliado `push` simulado.

Nunca registra el token del dispositivo ni lo incluye en URLs, encabezados visibles en logs o
errores. El 404 del aliado es un RECHAZADO solo si el cuerpo es exactamente
`{"error": "token_no_reconocido"}`; cualquier otro 404 es una respuesta inválida, igual que un
5xx (que la protección traduce) o un JSON mal formado.
"""

from typing import Any

import httpx
from pydantic import SecretStr

from adaptador_firma.domain.notificaciones.modelos import (
    Push,
    RespuestaInvalida,
    ResultadoPush,
    SolicitudPush,
)

_TOKEN_NO_RECONOCIDO = {"error": "token_no_reconocido"}


class PushHttp:
    def __init__(self, cliente: httpx.AsyncClient, base_url: str, api_key: SecretStr) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def enviar(self, solicitud: SolicitudPush, timeout_s: float) -> Push:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/envios",
            json={
                "tokenDispositivo": solicitud.token_dispositivo,
                "tipo": solicitud.tipo,
                "polizaId": str(solicitud.poliza_id),
                "eventId": str(solicitud.event_id),
            },
            headers={"X-Api-Key": self._api_key.get_secret_value()},
            timeout=timeout_s,
        )
        if respuesta.status_code == 404:
            return _token_no_reconocido(respuesta)
        if respuesta.status_code >= 500:
            respuesta.raise_for_status()
        if respuesta.status_code != 200:
            raise RespuestaInvalida(f"HTTP_{respuesta.status_code}")
        return _push(respuesta)


def _token_no_reconocido(respuesta: httpx.Response) -> Push:
    try:
        cuerpo = respuesta.json()
    except ValueError as error:
        raise RespuestaInvalida("HTTP_404") from error
    if cuerpo != _TOKEN_NO_RECONOCIDO:
        raise RespuestaInvalida("HTTP_404")
    return Push(resultado=ResultadoPush.RECHAZADO)


def _texto_opcional(valor: Any) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("envioId")
    return valor


def _push(respuesta: httpx.Response) -> Push:
    try:
        cuerpo = respuesta.json()
        if cuerpo["estado"] != "ENVIADO":
            raise ValueError("estado")
        return Push(resultado=ResultadoPush.OK, envio_id=_texto_opcional(cuerpo.get("envioId")))
    except (ValueError, KeyError, TypeError) as error:
        raise RespuestaInvalida("JSON") from error
