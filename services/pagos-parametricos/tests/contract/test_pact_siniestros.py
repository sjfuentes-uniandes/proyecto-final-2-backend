"""Pacto consumidor `pagos-parametricos` → proveedor `siniestros` (HU-W22, CT-W22-01, W22-B04).

Lo genera `make test-contract SERVICE=pagos-parametricos` en
`contracts/pact/pagos-parametricos-siniestros.json`. Cada interacción ejercita el cliente real
(`SiniestrosHttp.crear`) contra el mock de Pact: el pacto describe exactamente lo que
`pagos-parametricos` envía y lo que sabe leer (`id` y `codigo`). `siniestros` lo verifica como
proveedor (`make test-module SERVICE=siniestros`, `test_pacto_pagos_parametricos.py`).

Interacciones de `POST /v1/siniestros/parametricos` (DS3-23): la creación (201), la repetición (200,
el mismo siniestro) y la misma `Idempotency-Key` con otro cuerpo (422 `IDEMPOTENCIA_CONFLICTO`). Los
timeouts, los 5xx, el circuito y el *bulkhead* van en `tests/unit/test_siniestros_http.py`.

Es el mismo pacto que dejó `docs/sprint_3/evidencias/hu-w22/prototipos/generar_pacto.py` en B1
(R-186): el cliente real manda lo que ese script simulaba.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pact import Pact, match
from pagos_parametricos.adapters.outbound.siniestros_http import SiniestrosHttp
from pagos_parametricos.domain.parametrico.activacion import SolicitudSiniestro, clave_idempotente
from pagos_parametricos.domain.parametrico.errores import SiniestrosRechazo
from pagos_parametricos.domain.parametrico.modelos import EventoObservado
from solventa_common import correlation
from solventa_common.resiliencia import ProteccionDependencia

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "pagos-parametricos-siniestros.json"
SIN_SINIESTRO = "no existe un siniestro para ese evento y esa póliza"
CON_SINIESTRO = "ya existe el siniestro paramétrico de ese evento y esa póliza"
EVENTO = UUID("bafc2b26-7c88-5ae3-b5ac-26fdf5427a09")
POLIZA = UUID("b2100000-0000-4000-8000-000000000101")
CLIENTE = UUID("00000000-0000-4000-8000-000000000002")
SINIESTRO_ID = "5a000000-0000-4000-8000-000000000022"
CODIGO = "88-2026-1001"
CORRELACION = "corr-pacto-parametricos"
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
    pacto = Pact("pagos-parametricos", "siniestros").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


def _solicitud(valor: str = "1400000.00") -> SolicitudSiniestro:
    return SolicitudSiniestro(
        clave=clave_idempotente(EVENTO, POLIZA),
        evento_id=EVENTO,
        poliza_id=POLIZA,
        cliente_id=CLIENTE,
        numero="250-2026-000101",
        ramo="Paramétrico",
        vigencia_desde=date(2026, 9, 1),
        vigencia_hasta=date(2027, 8, 31),
        moneda="COP",
        evento=EventoObservado(
            EVENTO,
            "IDEAM-SIM",
            "IDEAM-2026-1110-0001",
            "EVT-2026-0913",
            "LLUVIA_ACUMULADA",
            Decimal(135),
            "mm",
            "11001",
            datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
        ),
        umbral="120",
        regla="VALOR_IGUAL_O_SUPERIOR_AL_UMBRAL",
        version_regla="1",
        valor=valor,
    )


def _pedido(pacto: Pact, descripcion: str, estado: str, solicitud: SolicitudSiniestro) -> Any:
    from pagos_parametricos.adapters.outbound.siniestros_http import cuerpo_de

    return (
        pacto.upon_receiving(descripcion)
        .given(estado)
        .with_request("POST", "/v1/siniestros/parametricos")
        .with_headers(
            {
                "Content-Type": "application/json",
                "X-Correlation-Id": CORRELACION,
                "Idempotency-Key": solicitud.clave,
                "X-Actor-Id": "pagos-parametricos",
                "X-Actor-Rol": "SISTEMA_PARAMETRICO",
            }
        )
        .with_body(cuerpo_de(solicitud), content_type="application/json")
    )


def _siniestro(*, existente: bool) -> dict[str, object]:
    return {
        "id": SINIESTRO_ID if existente else match.uuid(SINIESTRO_ID),
        "codigo": CODIGO if existente else match.regex(CODIGO, regex=r"^88-[0-9]{4}-[0-9]{4,}$"),
        "polizaId": str(POLIZA),
        "polizaNumero": "250-2026-000101",
        "clienteId": str(CLIENTE),
        "ramo": "Paramétrico",
        "origen": "PARAMETRICO",
        "estado": "APROBADO",
        "prioridad": match.regex("BAJA", regex=r"^(ALTA|MEDIA|BAJA)$"),
        "reportadoEn": match.regex(
            "2026-11-10T14:05:00Z", regex=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
        ),
        "analistaId": None,
        "tiempoRestanteMin": match.like(2880),
        "relojPausado": False,
    }


async def _crear(url: str, solicitud: SolicitudSiniestro) -> Any:
    token = correlation.establecer(CORRELACION)
    try:
        async with correlation.cliente_async() as cliente:
            return await SiniestrosHttp(cliente, url, 600, ProteccionDependencia(5, 30, 10)).crear(
                solicitud
            )
    finally:
        correlation.restablecer(token)


async def test_la_creacion_del_siniestro_de_un_evento_y_una_poliza(pacto: Pact) -> None:
    (
        _pedido(
            pacto,
            "la creación del siniestro paramétrico de un evento y una póliza",
            SIN_SINIESTRO,
            _solicitud(),
        )
        .will_respond_with(201)
        .with_header("Content-Type", "application/json")
        .with_body(_siniestro(existente=False), content_type="application/json")
    )
    with pacto.serve() as servidor:
        creado = await _crear(str(servidor.url), _solicitud())
    assert (str(creado.siniestro_id), creado.creado) == (SINIESTRO_ID, True)


async def test_la_repeticion_de_la_misma_solicitud_devuelve_el_mismo_siniestro(pacto: Pact) -> None:
    (
        _pedido(pacto, "la repetición de la misma solicitud", CON_SINIESTRO, _solicitud())
        .will_respond_with(200)
        .with_header("Content-Type", "application/json")
        .with_body(_siniestro(existente=True), content_type="application/json")
    )
    with pacto.serve() as servidor:
        repetido = await _crear(str(servidor.url), _solicitud())
    assert (str(repetido.siniestro_id), repetido.codigo, repetido.creado) == (
        SINIESTRO_ID,
        CODIGO,
        False,
    )


async def test_la_misma_clave_con_otro_cuerpo_es_un_conflicto(pacto: Pact) -> None:
    (
        _pedido(
            pacto,
            "la misma Idempotency-Key con otro cuerpo",
            CON_SINIESTRO,
            _solicitud("620000.00"),
        )
        .will_respond_with(422)
        .with_header("Content-Type", "application/json")
        .with_body(
            {
                "codigo": "IDEMPOTENCIA_CONFLICTO",
                "mensaje": match.like("La Idempotency-Key ya se usó con otra solicitud"),
                "correlationId": match.like(CORRELACION),
                "detalles": {},
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor, pytest.raises(SiniestrosRechazo) as rechazo:
        await _crear(str(servidor.url), _solicitud("620000.00"))
    assert (rechazo.value.estado, rechazo.value.codigo) == (422, "IDEMPOTENCIA_CONFLICTO")


def test_el_pacto_se_escribio_con_las_tres_interacciones() -> None:
    import json

    pacto = json.loads((PACTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert (pacto["consumer"]["name"], pacto["provider"]["name"]) == (
        "pagos-parametricos",
        "siniestros",
    )
    assert len(pacto["interactions"]) == 3
    assert httpx.__name__  # el cliente real es httpx
