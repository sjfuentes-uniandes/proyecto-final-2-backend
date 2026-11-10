"""Pacto consumidor `dinero` → proveedor `adaptador-pagos` para el cobro (HU-W16, 16.10), la
consulta de la conciliación (16.11) y, desde HU-W23, el desembolso de la indemnización y su
consulta.

Lo genera `make test-contract SERVICE=dinero` en `contracts/pact/dinero-adaptador-pagos.json`. Cada
interacción ejercita el adaptador real (`AdaptadorPagosHttp.cobrar` o `.consultar`) contra el mock
de Pact: el pacto describe exactamente lo que dinero envía y lo que sabe leer. `adaptador-pagos` lo
verifica como proveedor (`make test-contract SERVICE=adaptador-pagos`).

Solo lo que el proveedor promete: en el cobro, `200 APROBADO` y `200 RECHAZADO`; en la consulta
(`GET /v1/cobros/{claveRecaudo}?pagadorRef`), además, `404 COBRO_NO_ENCONTRADO`. Los timeouts, los
5xx y los cuerpos fuera de contrato van en `tests/unit/recaudo/test_pagos_http.py` y en las de
módulo.

Desembolsos (HU-W23): `POST /v1/desembolsos` con `referencia` (`IND-<codigo>-01`), el monto como
texto decimal, `COP` y el `beneficiarioRef` sintético: `200 CONFIRMADO` (con `transaccionId`) y
`200 RECHAZADO` (con `motivo`); y `GET /v1/desembolsos/{referencia}?beneficiarioRef` con `200
CONFIRMADO`, `200 RECHAZADO` y `404 DESEMBOLSO_NO_ENCONTRADO`. Los ejercita el cliente HTTP de la
indemnización de `dinero` (B3); hasta entonces, esta prueba envía la misma solicitud con el cliente
de correlación (R-113).
"""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from dinero.adapters.outbound.pagos_http import AdaptadorPagosHttp
from dinero.application.recaudo.puertos import (
    CobroAprobado,
    CobroNoEncontrado,
    CobroRechazado,
    ConsultaCobro,
    SolicitudCobro,
)
from pact import Pact, match
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "dinero-adaptador-pagos.json"
# Pagadores sintéticos C01 (aprobado) y C02 (rechazado) del simulador de pagos (16.7).
C01 = UUID("00000000-0000-4000-8000-000000000001")
C02 = UUID("00000000-0000-4000-8000-000000000002")
CLAVE_APROBADA = UUID("00000000-0000-4000-b000-000000000001")
CLAVE_RECHAZADA = UUID("00000000-0000-4000-b000-000000000002")
# 16.11: la consulta. C07 (caído en el cobro): el cobro nunca llegó al proveedor.
C07 = UUID("00000000-0000-4000-8000-000000000007")
CLAVE_CONSULTA_APROBADA = UUID("00000000-0000-4000-b000-000000000011")
CLAVE_CONSULTA_RECHAZADA = UUID("00000000-0000-4000-b000-000000000012")
CLAVE_NO_ENCONTRADA = UUID("00000000-0000-4000-b000-000000000017")
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


# --- HU-W16 (16.11): consulta de la conciliación ----------------------------------------------------
def _consulta(pacto: Pact, descripcion: str, estado: str, clave: UUID, pagador: UUID) -> Any:
    return (
        pacto.upon_receiving(descripcion)
        .given(estado)
        .with_request("GET", f"/v1/cobros/{clave}")
        .with_query_parameter("pagadorRef", str(pagador))
        .with_header("X-Correlation-Id", CORRELACION)
    )


async def _consultar(url: str, clave: UUID, pagador: UUID) -> Any:
    async with correlation.cliente_async() as cliente:
        return await AdaptadorPagosHttp(cliente, url, 6500).consultar(
            ConsultaCobro(clave_recaudo=clave, pagador_ref=pagador, correlation_id=CORRELACION)
        )


async def test_consulta_de_un_cobro_aprobado(pacto: Pact) -> None:
    (
        _consulta(
            pacto,
            "una consulta de un cobro que el proveedor aprobó",
            "el proveedor de pagos tiene el cobro aprobado",
            CLAVE_CONSULTA_APROBADA,
            C01,
        )
        .will_respond_with(200)
        .with_body(
            {
                "claveRecaudo": str(CLAVE_CONSULTA_APROBADA),
                "estado": "APROBADO",
                "transaccionId": match.like("TX-SINT-C01"),
                "motivo": None,
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        resultado = await _consultar(str(servidor.url), CLAVE_CONSULTA_APROBADA, C01)
    assert resultado == CobroAprobado("TX-SINT-C01")


async def test_consulta_de_un_cobro_rechazado(pacto: Pact) -> None:
    (
        _consulta(
            pacto,
            "una consulta de un cobro que el proveedor rechazó",
            "el proveedor de pagos tiene el cobro rechazado por fondos insuficientes",
            CLAVE_CONSULTA_RECHAZADA,
            C02,
        )
        .will_respond_with(200)
        .with_body(
            {
                "claveRecaudo": str(CLAVE_CONSULTA_RECHAZADA),
                "estado": "RECHAZADO",
                "transaccionId": None,
                "motivo": match.like("FONDOS_INSUFICIENTES"),
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        resultado = await _consultar(str(servidor.url), CLAVE_CONSULTA_RECHAZADA, C02)
    assert resultado == CobroRechazado(motivo="FONDOS_INSUFICIENTES", transaccion_id=None)


async def test_consulta_de_un_cobro_que_no_existe(pacto: Pact) -> None:
    """dinero solo lee `codigo`: el resto del cuerpo de error no es parte de lo que necesita."""
    (
        _consulta(
            pacto,
            "una consulta de un cobro que nunca llegó al proveedor",
            "el proveedor de pagos no tiene el cobro",
            CLAVE_NO_ENCONTRADA,
            C07,
        )
        .will_respond_with(404)
        .with_body({"codigo": "COBRO_NO_ENCONTRADO"}, content_type="application/json")
    )
    with pacto.serve() as servidor:
        resultado = await _consultar(str(servidor.url), CLAVE_NO_ENCONTRADA, C07)
    assert resultado == CobroNoEncontrado()


# --- HU-W23: desembolso de la indemnización y su consulta -------------------------------------------
# Beneficiarios sintéticos del simulador (ADR 0015): `ben-sint-001` confirma, `ben-sint-rechaza`
# rechaza y `ben-sint-caido` nunca recibió la orden.
BEN_CONFIRMA = "ben-sint-001"
BEN_RECHAZA = "ben-sint-rechaza"
BEN_CAIDO = "ben-sint-caido"
REF_CONFIRMADA = "IND-88-2026-0442-01"
REF_RECHAZADA = "IND-88-2026-0443-01"
REF_CONSULTA_CONFIRMADA = "IND-88-2026-0444-01"
REF_CONSULTA_RECHAZADA = "IND-88-2026-0445-01"
REF_NO_ENCONTRADA = "IND-88-2026-0446-01"


def _orden(pacto: Pact, descripcion: str, estado: str, referencia: str, beneficiario: str) -> Any:
    return (
        pacto.upon_receiving(descripcion)
        .given(estado)
        .with_request("POST", "/v1/desembolsos")
        .with_header("X-Correlation-Id", CORRELACION)
        .with_body(
            {
                "referencia": referencia,
                "monto": "2500000.00",
                "moneda": "COP",
                "beneficiarioRef": beneficiario,
            },
            content_type="application/json",
        )
    )


async def _ordenar(url: str, referencia: str, beneficiario: str) -> Any:
    async with correlation.cliente_async() as cliente:
        respuesta = await cliente.post(
            f"{url}/v1/desembolsos",
            json={
                "referencia": referencia,
                "monto": "2500000.00",
                "moneda": "COP",
                "beneficiarioRef": beneficiario,
            },
            headers={correlation.CORRELATION_HEADER: CORRELACION},
        )
    return respuesta.status_code, respuesta.json()


def _consulta_desembolso(
    pacto: Pact, descripcion: str, estado: str, referencia: str, beneficiario: str
) -> Any:
    return (
        pacto.upon_receiving(descripcion)
        .given(estado)
        .with_request("GET", f"/v1/desembolsos/{referencia}")
        .with_query_parameter("beneficiarioRef", beneficiario)
        .with_header("X-Correlation-Id", CORRELACION)
    )


async def _consultar_desembolso(url: str, referencia: str, beneficiario: str) -> Any:
    async with correlation.cliente_async() as cliente:
        respuesta = await cliente.get(
            f"{url}/v1/desembolsos/{referencia}",
            params={"beneficiarioRef": beneficiario},
            headers={correlation.CORRELATION_HEADER: CORRELACION},
        )
    return respuesta.status_code, respuesta.json()


async def test_desembolso_confirmado(pacto: Pact) -> None:
    (
        _orden(
            pacto,
            "una orden de desembolso que el proveedor confirma",
            "el proveedor de pagos confirma el desembolso",
            REF_CONFIRMADA,
            BEN_CONFIRMA,
        )
        .will_respond_with(200)
        .with_body(
            {
                # La misma referencia que se envió: dinero no acepta la orden de otra.
                "referencia": REF_CONFIRMADA,
                "estado": "CONFIRMADO",
                "transaccionId": match.like("TRX-SINT-1"),
                "motivo": None,
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        estado, cuerpo = await _ordenar(str(servidor.url), REF_CONFIRMADA, BEN_CONFIRMA)
    assert (estado, cuerpo["estado"], cuerpo["referencia"]) == (200, "CONFIRMADO", REF_CONFIRMADA)


async def test_desembolso_rechazado(pacto: Pact) -> None:
    (
        _orden(
            pacto,
            "una orden de desembolso que el proveedor rechaza",
            "el proveedor de pagos rechaza el desembolso",
            REF_RECHAZADA,
            BEN_RECHAZA,
        )
        .will_respond_with(200)
        .with_body(
            {
                "referencia": REF_RECHAZADA,
                "estado": "RECHAZADO",
                "transaccionId": None,
                "motivo": match.like("BENEFICIARIO_NO_HABILITADO"),
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        estado, cuerpo = await _ordenar(str(servidor.url), REF_RECHAZADA, BEN_RECHAZA)
    assert (estado, cuerpo["estado"], cuerpo["transaccionId"]) == (200, "RECHAZADO", None)


async def test_consulta_de_un_desembolso_confirmado(pacto: Pact) -> None:
    (
        _consulta_desembolso(
            pacto,
            "una consulta de un desembolso que el proveedor confirmó",
            "el proveedor de pagos tiene el desembolso confirmado",
            REF_CONSULTA_CONFIRMADA,
            BEN_CONFIRMA,
        )
        .will_respond_with(200)
        .with_body(
            {
                "referencia": REF_CONSULTA_CONFIRMADA,
                "estado": "CONFIRMADO",
                "transaccionId": match.like("TRX-SINT-1"),
                "motivo": None,
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        estado, cuerpo = await _consultar_desembolso(
            str(servidor.url), REF_CONSULTA_CONFIRMADA, BEN_CONFIRMA
        )
    assert (estado, cuerpo["estado"]) == (200, "CONFIRMADO")


async def test_consulta_de_un_desembolso_rechazado(pacto: Pact) -> None:
    (
        _consulta_desembolso(
            pacto,
            "una consulta de un desembolso que el proveedor rechazó",
            "el proveedor de pagos tiene el desembolso rechazado",
            REF_CONSULTA_RECHAZADA,
            BEN_RECHAZA,
        )
        .will_respond_with(200)
        .with_body(
            {
                "referencia": REF_CONSULTA_RECHAZADA,
                "estado": "RECHAZADO",
                "transaccionId": None,
                "motivo": match.like("BENEFICIARIO_NO_HABILITADO"),
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        estado, cuerpo = await _consultar_desembolso(
            str(servidor.url), REF_CONSULTA_RECHAZADA, BEN_RECHAZA
        )
    assert (estado, cuerpo["estado"]) == (200, "RECHAZADO")


async def test_consulta_de_un_desembolso_que_no_existe(pacto: Pact) -> None:
    """dinero solo lee `codigo`: el resto del cuerpo de error no es parte de lo que necesita."""
    (
        _consulta_desembolso(
            pacto,
            "una consulta de un desembolso que nunca llegó al proveedor",
            "el proveedor de pagos no tiene el desembolso",
            REF_NO_ENCONTRADA,
            BEN_CAIDO,
        )
        .will_respond_with(404)
        .with_body({"codigo": "DESEMBOLSO_NO_ENCONTRADO"}, content_type="application/json")
    )
    with pacto.serve() as servidor:
        estado, cuerpo = await _consultar_desembolso(
            str(servidor.url), REF_NO_ENCONTRADA, BEN_CAIDO
        )
    assert (estado, cuerpo["codigo"]) == (404, "DESEMBOLSO_NO_ENCONTRADO")
