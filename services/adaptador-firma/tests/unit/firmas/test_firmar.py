"""Caso de uso Firmar: timeout efectivo, errores y medición."""

import contextlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from adaptador_firma.application.firmas.firmar import Firmar
from adaptador_firma.domain.firmas.modelos import (
    EstadoFirma,
    Firma,
    RespuestaInvalida,
    SolicitudFirma,
)
from solventa_common.resiliencia import DependenciaNoDisponible, SinProteccion, TiempoAgotado

pytestmark = pytest.mark.anyio

DOCUMENTO = UUID("5b1c9e2a-7f3d-4a60-8c11-000000000d01")
HUELLA = "sha256:" + "0123456789abcdef" * 4
SOLICITUD = SolicitudFirma(
    documento_id=DOCUMENTO,
    huella=HUELLA,
    firmante_ref=UUID("00000000-0000-4000-8000-000000000001"),
)
FIRMADO = Firma(
    documento_id=DOCUMENTO,
    estado=EstadoFirma.FIRMADO,
    firma_id="FIR-SINT-C01",
    sello_tiempo=datetime(2026, 1, 1, tzinfo=UTC),
    huella=HUELLA,
)
_5XX = httpx.HTTPStatusError(
    "503", request=httpx.Request("POST", "http://sim"), response=httpx.Response(503)
)


class ProveedorFalso:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.firmas: list[tuple[SolicitudFirma, float]] = []

    async def firmar(self, solicitud: SolicitudFirma, timeout_s: float) -> Firma:
        self.firmas.append((solicitud, timeout_s))
        if self.error is not None:
            raise self.error
        return FIRMADO


async def _firmar(proveedor: ProveedorFalso, timeout_ms: int = 5000) -> Firma:
    caso = Firmar(
        proveedor=proveedor, proteccion=SinProteccion(corte_duro_ms=5000), timeout_ms=timeout_ms
    )
    return await caso.ejecutar(SOLICITUD)


async def test_firma_nominal_con_el_timeout_configurado() -> None:
    proveedor = ProveedorFalso()
    assert await _firmar(proveedor) == FIRMADO
    assert proveedor.firmas == [(SOLICITUD, 5.0)]


async def test_un_timeout_menor_que_el_corte_se_respeta() -> None:
    proveedor = ProveedorFalso()
    await _firmar(proveedor, timeout_ms=1200)
    assert proveedor.firmas[0][1] == 1.2


async def test_el_corte_duro_por_defecto_recortaria_a_700_ms() -> None:
    """Guardia: sin `corte_duro_ms`, SinProteccion corta en 700 ms aunque se configuren 5000."""
    proveedor = ProveedorFalso()
    caso = Firmar(proveedor=proveedor, proteccion=SinProteccion(), timeout_ms=5000)
    await caso.ejecutar(SOLICITUD)
    assert proveedor.firmas[0][1] == 0.7


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
        await _firmar(proveedor)
    # Sin reintentos: `polizas` reintenta con la misma clave idempotente (ADR 0014).
    assert len(proveedor.firmas) == 1


@pytest.mark.parametrize(
    ("proveedor", "esperado"),
    [
        (ProveedorFalso(), ("ok", 0)),
        (ProveedorFalso(error=httpx.ReadTimeout("lento")), ("timeout", 1)),
        (ProveedorFalso(error=httpx.ConnectError("caído")), ("error", 1)),
        (ProveedorFalso(error=_5XX), ("error", 1)),
        (ProveedorFalso(error=RespuestaInvalida("JSON")), ("error", 1)),
    ],
)
async def test_medicion_de_la_firma(
    telemetria: Any, proveedor: ProveedorFalso, esperado: tuple[str, int]
) -> None:
    """`firma.documento` es la operación de la alarma de degradación del proveedor (infra)."""
    with contextlib.suppress(Exception):
        await _firmar(proveedor)
    (span,) = [s for s in telemetria.lista_spans() if s.name == "firma.documento"]
    errores = [
        p
        for p in telemetria.puntos("OperationErrors")
        if p.attributes["Operation"] == "firma.documento"
    ]
    assert (span.attributes["result"], errores[0].value) == esperado
