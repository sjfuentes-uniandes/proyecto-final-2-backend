"""EnviadorPush sobre HTTPX, hacia `adaptador-firma` (`POST /v1/notificaciones`, HU-M11).

Sin `base_url` configurada, el servicio falla cerrado: `enviar` lanza `FalloTransitorio` sin llamar a
nadie, así que la entrega nunca se marca ENVIADO y sigue su ciclo normal de reintentos. Solo se
reintentan el timeout y los 5xx que traduce `adaptador-firma` (502/503/504); cualquier otra
respuesta inesperada se trata igual, como un fallo transitorio seguro.
"""

from typing import Any

import httpx
from solventa_common.resiliencia import (
    DependenciaNoDisponible,
    Proteccion,
    SinProteccion,
    TiempoAgotado,
)

from avisos.application.entregas.puertos import FalloTransitorio
from avisos.domain.avisos.modelos import (
    FALLO_ADAPTADOR_FIRMA,
    FALLO_SIN_CONFIGURAR,
    FALLO_TIEMPO_AGOTADO,
    Aviso,
)


class EnviadorPushHttp:
    def __init__(
        self,
        cliente: httpx.AsyncClient,
        base_url: str | None,
        timeout_ms: int,
        proteccion: Proteccion | None = None,
    ) -> None:
        self._cliente = cliente
        self._base_url = base_url.rstrip("/") if base_url else None
        self._timeout_ms = timeout_ms
        self._proteccion = proteccion or SinProteccion()

    async def enviar(self, token: str, aviso: Aviso) -> bool:
        if self._base_url is None:
            raise FalloTransitorio(FALLO_SIN_CONFIGURAR)

        async def llamada(timeout_s: float) -> bool:
            return await self._llamar(token, aviso, timeout_s)

        try:
            return await self._proteccion.ejecutar(
                clave="push",
                grupo="adaptador-firma",
                llamada=llamada,
                configurado_ms=self._timeout_ms,
                deadline_ms=None,
            )
        except TiempoAgotado as error:
            raise FalloTransitorio(FALLO_TIEMPO_AGOTADO) from error
        except DependenciaNoDisponible as error:
            raise FalloTransitorio(FALLO_ADAPTADOR_FIRMA) from error

    async def _llamar(self, token: str, aviso: Aviso, timeout_s: float) -> bool:
        respuesta = await self._cliente.post(
            f"{self._base_url}/v1/notificaciones",
            json={
                "tokenDispositivo": token,
                "tipo": aviso.tipo,
                "polizaId": str(aviso.poliza_id),
                "eventId": str(aviso.event_id),
            },
            timeout=timeout_s,
        )
        if respuesta.status_code >= 500:
            respuesta.raise_for_status()
        if respuesta.status_code != 200:
            raise FalloTransitorio(FALLO_ADAPTADOR_FIRMA)
        return _aceptado(respuesta)


def _aceptado(respuesta: httpx.Response) -> bool:
    try:
        cuerpo: Any = respuesta.json()
        resultado = cuerpo["resultado"]
        if resultado not in ("OK", "RECHAZADO"):
            raise ValueError("resultado")
        return resultado == "OK"
    except (ValueError, KeyError, TypeError) as error:
        raise FalloTransitorio(FALLO_ADAPTADOR_FIRMA) from error
