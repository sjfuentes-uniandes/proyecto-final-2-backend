"""PuertoProveedorFirma sobre HTTPX."""

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr

from adaptador_firma.domain.firmas.modelos import (
    EstadoFirma,
    Firma,
    RespuestaInvalida,
    SolicitudFirma,
)


class FirmaHttp:
    """Cliente del proveedor de firma simulado. Nunca registra la credencial, el firmante ni cuerpos.

    Los 5xx salen como HTTPStatusError y los de transporte como los de HTTPX: la protección los
    traduce. Cualquier otro estado distinto de 200 es una respuesta inválida.
    """

    def __init__(self, cliente: httpx.AsyncClient, base_url: str, api_key: SecretStr) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def firmar(self, solicitud: SolicitudFirma, timeout_s: float) -> Firma:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/firmas",
            json={
                "documentoId": str(solicitud.documento_id),
                "huella": solicitud.huella,
                "firmante": str(solicitud.firmante_ref),
            },
            headers={
                "X-Api-Key": self._api_key.get_secret_value(),
                # Reintentar la firma del mismo documento no firma otra vez (ADR 0014).
                "Idempotency-Key": str(solicitud.documento_id),
            },
            timeout=timeout_s,
        )
        if respuesta.status_code >= 500:
            respuesta.raise_for_status()
        if respuesta.status_code != 200:
            # Incluye el 404 `documento_no_reconocido`: no hay rechazo de la firma.
            raise RespuestaInvalida(f"HTTP_{respuesta.status_code}")
        return _firma(respuesta, solicitud)


def _texto(valor: Any) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("texto")
    return valor


def _con_zona(valor: Any) -> datetime:
    # Es la hora del proveedor: se exige su zona y no se compara con el reloj local.
    sello = datetime.fromisoformat(_texto(valor))
    if sello.utcoffset() is None:
        raise ValueError("selloTiempo")
    return sello


def _firma(respuesta: httpx.Response, solicitud: SolicitudFirma) -> Firma:
    """Solo vale la firma de lo enviado: el mismo documento y la misma huella, texto exacto."""
    try:
        cuerpo = respuesta.json()
        if UUID(_texto(cuerpo["documentoId"])) != solicitud.documento_id:
            raise ValueError("documentoId")
        if cuerpo["huella"] != solicitud.huella:
            raise ValueError("huella")
        return Firma(
            documento_id=solicitud.documento_id,
            estado=EstadoFirma(cuerpo["estado"]),
            firma_id=_texto(cuerpo["firmaId"]),
            sello_tiempo=_con_zona(cuerpo["selloTiempo"]),
            huella=solicitud.huella,
        )
    except (ValueError, KeyError, TypeError) as error:
        raise RespuestaInvalida("JSON") from error
