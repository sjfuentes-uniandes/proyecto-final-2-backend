"""N1 de HU-W05 en bff-web: la cotización del portal inicia el X-Deadline-Ms."""

import asyncio
import time
from uuid import UUID

import httpx
import pytest
from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.config import PRESUPUESTO_COTIZAR_MS, Settings
from pydantic import ValidationError

pytestmark = pytest.mark.anyio
CLAVE = UUID("00000000-0000-4000-c000-000000000001")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _cliente(solicitudes: list[httpx.Request], presupuesto_ms: int | None) -> CotizacionCliente:
    def responder(solicitud: httpx.Request) -> httpx.Response:
        solicitudes.append(solicitud)
        return httpx.Response(201, json={"id": "c"})

    return CotizacionCliente(
        "http://cotizacion:8080", 1500, httpx.MockTransport(responder), presupuesto_ms
    )


async def test_cotizar_envia_el_restante_del_presupuesto() -> None:
    solicitudes: list[httpx.Request] = []
    await _cliente(solicitudes, 900).cotizar({}, "asesor", CLAVE)
    assert 800 <= int(solicitudes[0].headers["X-Deadline-Ms"]) <= 900


async def test_sin_presupuesto_no_envia_el_encabezado() -> None:
    solicitudes: list[httpx.Request] = []
    await _cliente(solicitudes, None).cotizar({}, "asesor", CLAVE)
    assert "X-Deadline-Ms" not in solicitudes[0].headers


async def test_las_lecturas_no_llevan_deadline() -> None:
    solicitudes: list[httpx.Request] = []

    def responder(solicitud: httpx.Request) -> httpx.Response:
        solicitudes.append(solicitud)
        return httpx.Response(200, json={})

    cliente = CotizacionCliente("http://cotizacion:8080", 1500, httpx.MockTransport(responder), 900)
    await cliente.cotizacion(CLAVE, "asesor")
    assert "X-Deadline-Ms" not in solicitudes[0].headers


async def test_cotizacion_lenta_se_abandona_en_el_presupuesto() -> None:
    async def lenta(solicitud: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(201)  # pragma: no cover

    cliente = CotizacionCliente("http://cotizacion:8080", 1500, httpx.MockTransport(lenta), 150)
    inicio = time.perf_counter()
    with pytest.raises(TimeoutError):
        await cliente.cotizar({}, "asesor", CLAVE)
    assert (time.perf_counter() - inicio) * 1000 <= 150 + 80


def test_presupuesto_y_validacion_al_arrancar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LATENCY_BUDGET_MS", raising=False)
    assert Settings().presupuesto_cotizar_ms == PRESUPUESTO_COTIZAR_MS
    monkeypatch.setenv("LATENCY_BUDGET_MS", '{"portal.cotizar":1000}')
    monkeypatch.setenv("SOLVENTA_COTIZACION_TIMEOUT_MS", "1500")
    with pytest.raises(ValidationError, match="cotizacion_timeout_ms=1500"):
        Settings()
