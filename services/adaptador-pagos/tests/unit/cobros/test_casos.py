"""Casos de uso Cobrar y ConsultarCobro: timeout efectivo, errores y medición."""

import contextlib
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from adaptador_pagos.application.cobros.cobrar import Cobrar
from adaptador_pagos.application.cobros.consultar_cobro import ConsultarCobro
from adaptador_pagos.domain.cobros.modelos import (
    Cobro,
    CobroNoEncontrado,
    EstadoCobro,
    RespuestaInvalida,
    SolicitudCobro,
)
from solventa_common.resiliencia import DependenciaNoDisponible, SinProteccion, TiempoAgotado

pytestmark = pytest.mark.anyio

CLAVE = UUID("7d3f6a2e-0c1b-4b8e-9f00-000000000a01")
PAGADOR = UUID("00000000-0000-4000-8000-000000000001")
SOLICITUD = SolicitudCobro(
    clave_recaudo=CLAVE, monto=Decimal("64638.00"), moneda="COP", pagador_ref=PAGADOR
)
APROBADO = Cobro(
    clave_recaudo=CLAVE, estado=EstadoCobro.APROBADO, transaccion_id="TX-SINT-C01", motivo=None
)
RECHAZADO = Cobro(
    clave_recaudo=CLAVE,
    estado=EstadoCobro.RECHAZADO,
    transaccion_id=None,
    motivo="FONDOS_INSUFICIENTES",
)
_5XX = httpx.HTTPStatusError(
    "503", request=httpx.Request("POST", "http://sim"), response=httpx.Response(503)
)


class ProveedorFalso:
    def __init__(self, respuesta: Cobro = APROBADO, error: BaseException | None = None) -> None:
        self.respuesta = respuesta
        self.error = error
        self.cobros: list[tuple[SolicitudCobro, float]] = []
        self.consultas: list[tuple[UUID, UUID, float]] = []

    async def cobrar(self, solicitud: SolicitudCobro, timeout_s: float) -> Cobro:
        self.cobros.append((solicitud, timeout_s))
        if self.error is not None:
            raise self.error
        return self.respuesta

    async def consultar(self, clave_recaudo: UUID, pagador_ref: UUID, timeout_s: float) -> Cobro:
        self.consultas.append((clave_recaudo, pagador_ref, timeout_s))
        if self.error is not None:
            raise self.error
        return self.respuesta


def _proteccion() -> SinProteccion:
    return SinProteccion(corte_duro_ms=5000)


async def _cobrar(proveedor: ProveedorFalso, timeout_ms: int = 5000) -> Cobro:
    caso = Cobrar(proveedor=proveedor, proteccion=_proteccion(), timeout_ms=timeout_ms)
    return await caso.ejecutar(SOLICITUD)


async def _consultar(proveedor: ProveedorFalso, timeout_ms: int = 5000) -> Cobro:
    caso = ConsultarCobro(proveedor=proveedor, proteccion=_proteccion(), timeout_ms=timeout_ms)
    return await caso.ejecutar(CLAVE, PAGADOR)


async def test_cobro_nominal_con_el_timeout_configurado() -> None:
    proveedor = ProveedorFalso()
    assert await _cobrar(proveedor) == APROBADO
    assert proveedor.cobros == [(SOLICITUD, 5.0)]


async def test_consulta_nominal_con_el_timeout_configurado() -> None:
    proveedor = ProveedorFalso(respuesta=RECHAZADO)
    assert await _consultar(proveedor, timeout_ms=4000) == RECHAZADO
    assert proveedor.consultas == [(CLAVE, PAGADOR, 4.0)]


async def test_el_corte_duro_por_defecto_recortaria_a_700_ms() -> None:
    """Guardia: sin `corte_duro_ms`, SinProteccion corta en 700 ms aunque se configuren 5000."""
    proveedor = ProveedorFalso()
    caso = Cobrar(proveedor=proveedor, proteccion=SinProteccion(), timeout_ms=5000)
    await caso.ejecutar(SOLICITUD)
    assert proveedor.cobros[0][1] == 0.7


@pytest.mark.parametrize("llamar", [_cobrar, _consultar])
@pytest.mark.parametrize(
    ("error", "esperado"),
    [
        (httpx.ReadTimeout("lento"), TiempoAgotado),
        (httpx.ConnectError("caído"), DependenciaNoDisponible),
        (_5XX, DependenciaNoDisponible),
        (RespuestaInvalida("JSON"), RespuestaInvalida),
    ],
)
async def test_errores(llamar: Any, error: BaseException, esperado: type[BaseException]) -> None:
    proveedor = ProveedorFalso(error=error)
    with pytest.raises(esperado):
        await llamar(proveedor)
    # Sin reintentos: la incertidumbre la resuelven dinero y la conciliación.
    assert len(proveedor.cobros) + len(proveedor.consultas) == 1


async def test_cobro_no_encontrado_se_propaga() -> None:
    with pytest.raises(CobroNoEncontrado):
        await _consultar(ProveedorFalso(error=CobroNoEncontrado()))


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
async def test_medicion_del_cobro(
    telemetria: Any, proveedor: ProveedorFalso, esperado: tuple[str, int]
) -> None:
    """`cobro.pagos` es la operación de la alarma de degradación del proveedor (infra)."""
    with contextlib.suppress(Exception):
        await _cobrar(proveedor)
    assert _resultado(telemetria, "cobro.pagos") == esperado


@pytest.mark.parametrize(
    ("proveedor", "esperado"),
    [
        (ProveedorFalso(), ("ok", 0)),
        (ProveedorFalso(respuesta=RECHAZADO), ("ok", 0)),
        (ProveedorFalso(error=CobroNoEncontrado()), ("rechazado", 0)),
        (ProveedorFalso(error=httpx.ReadTimeout("lento")), ("timeout", 1)),
        (ProveedorFalso(error=httpx.ConnectError("caído")), ("error", 1)),
        (ProveedorFalso(error=RespuestaInvalida("JSON")), ("error", 1)),
    ],
)
async def test_medicion_de_la_consulta(
    telemetria: Any, proveedor: ProveedorFalso, esperado: tuple[str, int]
) -> None:
    with contextlib.suppress(Exception):
        await _consultar(proveedor)
    assert _resultado(telemetria, "conciliacion.pagos") == esperado
