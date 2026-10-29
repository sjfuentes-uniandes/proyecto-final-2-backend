"""Pacto consumidor `dinero` → proveedor `adaptador-pagos` para el cobro (HU-W16, 16.10).

Lo genera `make test-contract SERVICE=dinero` en `contracts/pact/dinero-adaptador-pagos.json`. Cada
interacción ejercita el adaptador real (`AdaptadorPagosHttp.cobrar`) contra el mock de Pact: el
pacto describe exactamente lo que dinero envía y lo que sabe leer. `adaptador-pagos` lo verifica
como proveedor (`make test-contract SERVICE=adaptador-pagos`).

Solo lo que el proveedor promete: `200 APROBADO` y `200 RECHAZADO`. Los timeouts, los 5xx y los
cuerpos fuera de contrato van en `tests/unit/recaudo/test_pagos_http.py` y en las de módulo. La
consulta (`GET /v1/cobros/{claveRecaudo}`) entra con la conciliación (16.11).
"""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from dinero.adapters.outbound.pagos_http import AdaptadorPagosHttp
from dinero.application.recaudo.puertos import CobroAprobado, CobroRechazado, SolicitudCobro
from pact import Pact, match
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "dinero-adaptador-pagos.json"
# Pagadores sintéticos C01 (aprobado) y C02 (rechazado) del simulador de pagos (16.7).
C01 = UUID("00000000-0000-4000-8000-000000000001")
C02 = UUID("00000000-0000-4000-8000-000000000002")
CLAVE_APROBADA = UUID("00000000-0000-4000-b000-000000000001")
CLAVE_RECHAZADA = UUID("00000000-0000-4000-b000-000000000002")
CORRELACION = "corr-pacto-dinero"
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    # Un mock por interacción; write_file las combina.
    pacto = Pact("dinero", "adaptador-pagos").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


def _pedido(pacto: Pact, descripcion: str, estado: str, clave: UUID, pagador: UUID) -> Any:
    return (
        pacto.upon_receiving(descripcion)
        .given(estado)
        .with_request("POST", "/v1/cobros")
        .with_header("X-Correlation-Id", CORRELACION)
        .with_body(
            {
                "claveRecaudo": str(clave),
                "monto": "64638.00",
                "moneda": "COP",
                "pagadorRef": str(pagador),
            },
            content_type="application/json",
        )
    )


async def _cobrar(url: str, clave: UUID, pagador: UUID) -> Any:
    async with correlation.cliente_async() as cliente:
        return await AdaptadorPagosHttp(cliente, url, 6500).cobrar(
            SolicitudCobro(
                clave_recaudo=clave,
                monto=Decimal("64638.00"),
                moneda="COP",
                pagador_ref=pagador,
                correlation_id=CORRELACION,
            )
        )


async def test_cobro_aprobado(pacto: Pact) -> None:
    (
        _pedido(
            pacto,
            "un cobro que el proveedor aprueba",
            "el proveedor de pagos aprueba el cobro",
            CLAVE_APROBADA,
            C01,
        )
        .will_respond_with(200)
        .with_body(
            {
                # La misma clave que se envió: dinero no acepta el cobro de otra.
                "claveRecaudo": str(CLAVE_APROBADA),
                "estado": "APROBADO",
                "transaccionId": match.like("TX-SINT-C01"),
                "motivo": None,
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        resultado = await _cobrar(str(servidor.url), CLAVE_APROBADA, C01)
    assert resultado == CobroAprobado("TX-SINT-C01")


async def test_cobro_rechazado(pacto: Pact) -> None:
    (
        _pedido(
            pacto,
            "un cobro que el proveedor rechaza",
            "el proveedor de pagos rechaza el cobro por fondos insuficientes",
            CLAVE_RECHAZADA,
            C02,
        )
        .will_respond_with(200)
        .with_body(
            {
                "claveRecaudo": str(CLAVE_RECHAZADA),
                "estado": "RECHAZADO",
                "transaccionId": None,
                "motivo": match.like("FONDOS_INSUFICIENTES"),
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        resultado = await _cobrar(str(servidor.url), CLAVE_RECHAZADA, C02)
    assert resultado == CobroRechazado(motivo="FONDOS_INSUFICIENTES", transaccion_id=None)
