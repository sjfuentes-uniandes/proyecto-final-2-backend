"""PuertoProveedorPagos sobre HTTPX."""

from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr

from adaptador_pagos.domain.cobros.modelos import (
    Cobro,
    CobroNoEncontrado,
    EstadoCobro,
    RespuestaInvalida,
    SolicitudCobro,
)


class PagosHttp:
    """Cliente del proveedor de pagos simulado. Nunca registra la credencial, el pagador ni cuerpos.

    Los 5xx salen como HTTPStatusError y los de transporte como los de HTTPX: la protección los
    traduce. Cualquier otro estado distinto de 200 es una respuesta inválida, no un rechazo.
    """

    def __init__(self, cliente: httpx.AsyncClient, base_url: str, api_key: SecretStr) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def cobrar(self, solicitud: SolicitudCobro, timeout_s: float) -> Cobro:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/cobros",
            json={
                "referencia": str(solicitud.clave_recaudo),
                "monto": str(solicitud.monto),
                "moneda": solicitud.moneda,
                "pagador": str(solicitud.pagador_ref),
            },
            headers={
                "X-Api-Key": self._api_key.get_secret_value(),
                "Idempotency-Key": str(solicitud.clave_recaudo),
            },
            timeout=timeout_s,
        )
        _exigir_200(respuesta)
        return _cobro(respuesta, solicitud.clave_recaudo)

    async def consultar(self, clave_recaudo: UUID, pagador_ref: UUID, timeout_s: float) -> Cobro:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/cobros",
            params={"referencia": str(clave_recaudo), "pagador": str(pagador_ref)},
            headers={"X-Api-Key": self._api_key.get_secret_value()},
            timeout=timeout_s,
        )
        if respuesta.status_code == 404 and _codigo_de_error(respuesta) == "cobro_no_encontrado":
            raise CobroNoEncontrado
        _exigir_200(respuesta)
        return _cobro(respuesta, clave_recaudo)


def _exigir_200(respuesta: httpx.Response) -> None:
    if respuesta.status_code >= 500:
        respuesta.raise_for_status()
    if respuesta.status_code != 200:
        # Incluye el 404 `referencia_no_reconocida`: no es un rechazo del cobro.
        raise RespuestaInvalida(f"HTTP_{respuesta.status_code}")


def _codigo_de_error(respuesta: httpx.Response) -> Any:
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return None
    return cuerpo.get("error") if isinstance(cuerpo, dict) else None


def _texto_o_nulo(valor: Any) -> str | None:
    if valor is not None and (not isinstance(valor, str) or not valor):
        raise ValueError("texto")
    return valor


def _cobro(respuesta: httpx.Response, clave_recaudo: UUID) -> Cobro:
    try:
        cuerpo = respuesta.json()
        referencia = cuerpo["referencia"]
        if not isinstance(referencia, str) or UUID(referencia) != clave_recaudo:
            raise ValueError("referencia")
        estado = EstadoCobro(cuerpo["estado"])
        transaccion_id = _texto_o_nulo(cuerpo.get("transaccionId"))
        if estado is EstadoCobro.APROBADO and transaccion_id is None:
            raise ValueError("transaccionId")
        return Cobro(
            clave_recaudo=clave_recaudo,
            estado=estado,
            transaccion_id=transaccion_id,
            motivo=_texto_o_nulo(cuerpo.get("motivo")),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise RespuestaInvalida("JSON") from error
