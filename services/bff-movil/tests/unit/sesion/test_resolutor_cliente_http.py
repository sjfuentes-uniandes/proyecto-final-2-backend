"""Adaptador del resolutor del `sub` hacia el clienteId (GET /v1/clientes/actual)."""

from uuid import UUID

import httpx
import pytest
from bff_movil.adapters.outbound.resolutor_cliente_http import ResolutorClienteHttp
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible
from solventa_common import correlation

pytestmark = pytest.mark.anyio

CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")


def _resolutor(manejador, base_url: str = "http://clientes.test") -> ResolutorClienteHttp:
    cliente = correlation.cliente_async(transport=httpx.MockTransport(manejador))
    return ResolutorClienteHttp(cliente, base_url, timeout_ms=500)


async def test_resuelve_el_cliente_del_titular_con_los_encabezados_del_actor() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(
            200,
            json={"clienteId": str(CLIENTE_ID), "estado": "PENDIENTE", "verificadoEn": None},
        )

    assert await _resolutor(manejador).resolver("sub-1") == CLIENTE_ID
    assert recibidas[0].url.path == "/v1/clientes/actual"
    assert recibidas[0].headers["X-Actor-Id"] == "sub-1"
    assert recibidas[0].headers["X-Actor-Rol"] == "CLIENTE"


async def test_un_404_es_cliente_no_encontrado() -> None:
    with pytest.raises(ClienteNoEncontrado):
        await _resolutor(lambda _: httpx.Response(404)).resolver("sub-1")


async def test_un_timeout_no_se_reintenta() -> None:
    llamadas = 0

    def manejador(_: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        raise httpx.ReadTimeout("sin respuesta")

    with pytest.raises(ServicioNoDisponible) as error:
        await _resolutor(manejador).resolver("sub-1")

    assert error.value.motivo == "TIEMPO_AGOTADO"
    assert llamadas == 1


@pytest.mark.parametrize(
    "respuesta",
    [httpx.Response(503), httpx.Response(200, text="<html>"), httpx.Response(200, json={})],
)
async def test_respuestas_inesperadas_son_servicio_no_disponible(respuesta: httpx.Response) -> None:
    with pytest.raises(ServicioNoDisponible) as error:
        await _resolutor(lambda _: respuesta).resolver("sub-1")

    assert error.value.motivo == "ERROR"
