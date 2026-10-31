"""Caso de uso EnviarPush: timeout efectivo, errores, rechazo y medición."""

import contextlib
from typing import Any
from uuid import UUID

import httpx
import pytest
from adaptador_firma.application.notificaciones.enviar import EnviarPush
from adaptador_firma.domain.notificaciones.modelos import (
    Push,
    RespuestaInvalida,
    ResultadoPush,
    SolicitudPush,
)
from solventa_common.resiliencia import DependenciaNoDisponible, SinProteccion, TiempoAgotado

pytestmark = pytest.mark.anyio

POLIZA = UUID("5b1c9e2a-7f3d-4a60-8c11-000000000d01")
EVENTO = UUID("00000000-0000-4000-8000-000000000e01")
SOLICITUD = SolicitudPush(
    token_dispositivo="token-sintetico-entregado",
    tipo="POLIZA_EMITIDA",
    poliza_id=POLIZA,
    event_id=EVENTO,
)
ACEPTADO = Push(resultado=ResultadoPush.OK, envio_id="PUSH-1")
RECHAZADO = Push(resultado=ResultadoPush.RECHAZADO)
_5XX = httpx.HTTPStatusError(
    "503", request=httpx.Request("POST", "http://sim"), response=httpx.Response(503)
)


class ProveedorFalso:
    def __init__(self, resultado: Push = ACEPTADO, error: BaseException | None = None) -> None:
        self.resultado = resultado
        self.error = error
        self.enviados: list[tuple[SolicitudPush, float]] = []

    async def enviar(self, solicitud: SolicitudPush, timeout_s: float) -> Push:
        self.enviados.append((solicitud, timeout_s))
        if self.error is not None:
            raise self.error
        return self.resultado


async def _enviar(proveedor: ProveedorFalso, timeout_ms: int = 2000) -> Push:
    caso = EnviarPush(
        proveedor=proveedor, proteccion=SinProteccion(corte_duro_ms=2000), timeout_ms=timeout_ms
    )
    return await caso.ejecutar(SOLICITUD)


async def test_push_nominal_con_el_timeout_configurado() -> None:
    proveedor = ProveedorFalso()
    assert await _enviar(proveedor) == ACEPTADO
    assert proveedor.enviados == [(SOLICITUD, 2.0)]


async def test_un_timeout_menor_que_el_corte_se_respeta() -> None:
    proveedor = ProveedorFalso()
    await _enviar(proveedor, timeout_ms=1200)
    assert proveedor.enviados[0][1] == 1.2


async def test_token_rechazado_no_lanza_y_devuelve_el_resultado() -> None:
    proveedor = ProveedorFalso(resultado=RECHAZADO)
    assert await _enviar(proveedor) == RECHAZADO


@pytest.mark.parametrize(
    ("error", "esperado"),
    [
        (httpx.ReadTimeout("lento"), TiempoAgotado),
        (httpx.ConnectError("caído"), DependenciaNoDisponible),
        (_5XX, DependenciaNoDisponible),
        (RespuestaInvalida("JSON"), RespuestaInvalida),
    ],
)
async def test_errores_sin_reintentos(error: BaseException, esperado: type[BaseException]) -> None:
    proveedor = ProveedorFalso(error=error)
    with pytest.raises(esperado):
        await _enviar(proveedor)
    assert len(proveedor.enviados) == 1


@pytest.mark.parametrize(
    ("proveedor", "esperado"),
    [
        (ProveedorFalso(), ("ok", 0)),
        (ProveedorFalso(resultado=RECHAZADO), ("rechazado", 0)),
        (ProveedorFalso(error=httpx.ReadTimeout("lento")), ("timeout", 1)),
        (ProveedorFalso(error=httpx.ConnectError("caído")), ("error", 1)),
        (ProveedorFalso(error=_5XX), ("error", 1)),
        (ProveedorFalso(error=RespuestaInvalida("JSON")), ("error", 1)),
    ],
)
async def test_medicion_del_push(
    telemetria: Any, proveedor: ProveedorFalso, esperado: tuple[str, int]
) -> None:
    """`notificacion.push` es la operación de la alarma de degradación del proveedor (infra); un
    rechazo no cuenta como error del proveedor."""
    with contextlib.suppress(Exception):
        await _enviar(proveedor)
    (span,) = [s for s in telemetria.lista_spans() if s.name == "notificacion.push"]
    errores = [
        p
        for p in telemetria.puntos("OperationErrors")
        if p.attributes["Operation"] == "notificacion.push"
    ]
    assert (span.attributes["result"], errores[0].value) == esperado
