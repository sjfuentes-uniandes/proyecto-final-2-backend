"""PuertoProveedorDesembolsos sobre HTTPX (HU-W23): el aliado `pagos` del simulador.

Mismo criterio que `PagosHttp`: nunca registra la credencial, el beneficiario ni cuerpos. Los 5xx
salen como HTTPStatusError y los de transporte como los de HTTPX (la protección los traduce).
Cualquier otro estado distinto de 200 es una respuesta inválida, no un rechazo.
"""

import httpx
from pydantic import SecretStr

from adaptador_pagos.adapters.outbound.pagos_http import (
    _codigo_de_error,
    _exigir_200,
    _texto_o_nulo,
)
from adaptador_pagos.domain.cobros.modelos import RespuestaInvalida
from adaptador_pagos.domain.desembolsos.modelos import (
    Desembolso,
    DesembolsoNoEncontrado,
    EstadoDesembolso,
    SolicitudDesembolso,
)


class DesembolsosHttp:
    def __init__(self, cliente: httpx.AsyncClient, base_url: str, api_key: SecretStr) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def desembolsar(self, solicitud: SolicitudDesembolso, timeout_s: float) -> Desembolso:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/desembolsos",
            json={
                "referencia": solicitud.referencia,
                "monto": str(solicitud.monto),
                "moneda": solicitud.moneda,
                "beneficiario": solicitud.beneficiario_ref,
            },
            headers={
                "X-Api-Key": self._api_key.get_secret_value(),
                "Idempotency-Key": solicitud.referencia,
            },
            timeout=timeout_s,
        )
        _exigir_200(respuesta)
        return _desembolso(respuesta, solicitud.referencia)

    async def consultar(
        self, referencia: str, beneficiario_ref: str, timeout_s: float
    ) -> Desembolso:
        respuesta = await self._cliente.get(
            f"{self._base_url}/v1/desembolsos",
            params={"referencia": referencia, "beneficiario": beneficiario_ref},
            headers={"X-Api-Key": self._api_key.get_secret_value()},
            timeout=timeout_s,
        )
        if (
            respuesta.status_code == 404
            and _codigo_de_error(respuesta) == "desembolso_no_encontrado"
        ):
            raise DesembolsoNoEncontrado
        _exigir_200(respuesta)
        return _desembolso(respuesta, referencia)


def _desembolso(respuesta: httpx.Response, referencia: str) -> Desembolso:
    try:
        cuerpo = respuesta.json()
        if cuerpo["referencia"] != referencia:
            raise ValueError("referencia")
        estado = EstadoDesembolso(cuerpo["estado"])
        transaccion_id = _texto_o_nulo(cuerpo.get("transaccionId"))
        if estado is EstadoDesembolso.CONFIRMADO and transaccion_id is None:
            raise ValueError("transaccionId")
        return Desembolso(
            referencia=referencia,
            estado=estado,
            transaccion_id=transaccion_id,
            motivo=_texto_o_nulo(cuerpo.get("motivo")),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise RespuestaInvalida("JSON") from error
