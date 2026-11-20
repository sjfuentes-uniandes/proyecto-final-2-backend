"""Casos de uso Desembolsar y ConsultarDesembolso: timeout efectivo, errores y medición."""

import contextlib
from decimal import Decimal
from typing import Any

import httpx
import pytest
from adaptador_pagos.application.desembolsos.consultar_desembolso import ConsultarDesembolso
from adaptador_pagos.application.desembolsos.desembolsar import Desembolsar
from adaptador_pagos.domain.cobros.modelos import RespuestaInvalida
from adaptador_pagos.domain.desembolsos.modelos import (
    Desembolso,
    DesembolsoNoEncontrado,
    EstadoDesembolso,
    SolicitudDesembolso,
)
from solventa_common.resiliencia import DependenciaNoDisponible, SinProteccion, TiempoAgotado

pytestmark = pytest.mark.anyio

REFERENCIA = "IND-88-2026-0442-01"
BENEFICIARIO = "ben-sint-001"
SOLICITUD = SolicitudDesembolso(
    referencia=REFERENCIA, monto=Decimal("2500000.00"), moneda="COP", beneficiario_ref=BENEFICIARIO
)
CONFIRMADO = Desembolso(
    referencia=REFERENCIA,
    estado=EstadoDesembolso.CONFIRMADO,
    transaccion_id="TRX-SINT-1",
    motivo=None,
)
RECHAZADO = Desembolso(
    referencia=REFERENCIA,
    estado=EstadoDesembolso.RECHAZADO,
    transaccion_id=None,
    motivo="BENEFICIARIO_NO_HABILITADO",
)
_5XX = httpx.HTTPStatusError(
    "503", request=httpx.Request("POST", "http://sim"), response=httpx.Response(503)
)


class ProveedorFalso:
    def __init__(
        self, respuesta: Desembolso = CONFIRMADO, error: BaseException | None = None
    ) -> None:
        self.respuesta = respuesta
        self.error = error
        self.ordenes: list[tuple[SolicitudDesembolso, float]] = []
        self.consultas: list[tuple[str, str, float]] = []

    async def desembolsar(self, solicitud: SolicitudDesembolso, timeout_s: float) -> Desembolso:
        self.ordenes.append((solicitud, timeout_s))
        if self.error is not None:
            raise self.error
        return self.respuesta

    async def consultar(
        self, referencia: str, beneficiario_ref: str, timeout_s: float
    ) -> Desembolso:
        self.consultas.append((referencia, beneficiario_ref, timeout_s))
        if self.error is not None:
            raise self.error
        return self.respuesta


def _proteccion() -> SinProteccion:
    return SinProteccion(corte_duro_ms=5000)


async def _desembolsar(proveedor: ProveedorFalso, timeout_ms: int = 5000) -> Desembolso:
    caso = Desembolsar(proveedor=proveedor, proteccion=_proteccion(), timeout_ms=timeout_ms)
    return await caso.ejecutar(SOLICITUD)


async def _consultar(proveedor: ProveedorFalso, timeout_ms: int = 5000) -> Desembolso:
    caso = ConsultarDesembolso(proveedor=proveedor, proteccion=_proteccion(), timeout_ms=timeout_ms)
    return await caso.ejecutar(REFERENCIA, BENEFICIARIO)


async def test_desembolso_nominal_con_el_timeout_configurado() -> None:
    proveedor = ProveedorFalso()
    assert await _desembolsar(proveedor) == CONFIRMADO
    assert proveedor.ordenes == [(SOLICITUD, 5.0)]


async def test_consulta_nominal_con_el_timeout_configurado() -> None:
    proveedor = ProveedorFalso(respuesta=RECHAZADO)
    assert await _consultar(proveedor, timeout_ms=4000) == RECHAZADO
    assert proveedor.consultas == [(REFERENCIA, BENEFICIARIO, 4.0)]


@pytest.mark.parametrize("llamar", [_desembolsar, _consultar])
@pytest.mark.parametrize(
    ("error", "esperado"),
    [
        (httpx.ReadTimeout("lento"), TiempoAgotado),
        (httpx.ConnectError("caído"), DependenciaNoDisponible),
        (_5XX, DependenciaNoDisponible),
        (RespuestaInvalida("JSON"), RespuestaInvalida),
    ],
)
async def test_errores_sin_reintentos(
    llamar: Any, error: BaseException, esperado: type[BaseException]
) -> None:
    proveedor = ProveedorFalso(error=error)
    with pytest.raises(esperado):
        await llamar(proveedor)
    # Una sola llamada: la incertidumbre la resuelven dinero y su conciliación.
    assert len(proveedor.ordenes) + len(proveedor.consultas) == 1


async def test_desembolso_no_encontrado_se_propaga() -> None:
    with pytest.raises(DesembolsoNoEncontrado):
        await _consultar(ProveedorFalso(error=DesembolsoNoEncontrado()))


def _resultado(telemetria: Any, operacion: str) -> tuple[str, int]:
    (span,) = [s for s in telemetria.lista_spans() if s.name == operacion]
    errores = [
        p for p in telemetria.puntos("OperationErrors") if p.attributes["Operation"] == operacion
    ]
    return span.attributes["result"], errores[0].value


@pytest.mark.parametrize(
    ("proveedor", "esperado"),
    [
        (ProveedorFalso(), ("ok", 0)),
        (ProveedorFalso(respuesta=RECHAZADO), ("rechazado", 0)),
        (ProveedorFalso(error=httpx.ReadTimeout("lento")), ("timeout", 1)),
        (ProveedorFalso(error=_5XX), ("error", 1)),
        (ProveedorFalso(error=RespuestaInvalida("JSON")), ("error", 1)),
    ],
)
async def test_medicion_de_desembolso_ordenar(
    telemetria: Any, proveedor: ProveedorFalso, esperado: tuple[str, int]
) -> None:
    with contextlib.suppress(Exception):
        await _desembolsar(proveedor)
    assert _resultado(telemetria, "desembolso.ordenar") == esperado


@pytest.mark.parametrize(
    ("proveedor", "esperado"),
    [
        (ProveedorFalso(), ("ok", 0)),
        (ProveedorFalso(respuesta=RECHAZADO), ("ok", 0)),
        (ProveedorFalso(error=DesembolsoNoEncontrado()), ("rechazado", 0)),
        (ProveedorFalso(error=httpx.ReadTimeout("lento")), ("timeout", 1)),
        (ProveedorFalso(error=httpx.ConnectError("caído")), ("error", 1)),
        (ProveedorFalso(error=RespuestaInvalida("JSON")), ("error", 1)),
    ],
)
async def test_medicion_de_desembolso_consultar(
    telemetria: Any, proveedor: ProveedorFalso, esperado: tuple[str, int]
) -> None:
    with contextlib.suppress(Exception):
        await _consultar(proveedor)
    assert _resultado(telemetria, "desembolso.consultar") == esperado
