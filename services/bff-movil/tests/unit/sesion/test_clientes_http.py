"""Adaptador de `clientes`: ruta, encabezados y traducción de errores."""

from uuid import UUID

import httpx
import pytest
from bff_movil.adapters.outbound.clientes_http import ClientesHttp
from bff_movil.domain.sesion.errores import ClienteNoEncontrado, ServicioNoDisponible
from bff_movil.domain.sesion.modelos import EstadoIdentidad
from solventa_common import correlation

pytestmark = pytest.mark.anyio

CLIENTE_ID = UUID("00000000-0000-4000-8000-000000000001")


def _adaptador(manejador, base_url: str = "http://clientes.test") -> ClientesHttp:
    cliente = correlation.cliente_async(transport=httpx.MockTransport(manejador))
    return ClientesHttp(cliente, base_url, timeout_ms=500)


async def test_consulta_la_identidad_con_los_encabezados_del_actor() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"clienteId": str(CLIENTE_ID), "estado": "PENDIENTE"})

    estado = await _adaptador(manejador).obtener(CLIENTE_ID, "actor-1")

    assert estado is EstadoIdentidad.PENDIENTE
    assert recibidas[0].url.path == f"/v1/clientes/{CLIENTE_ID}/identidad"
    assert recibidas[0].headers["X-Actor-Id"] == "actor-1"
    assert recibidas[0].headers["X-Actor-Rol"] == "CLIENTE"


async def test_respeta_el_prefijo_de_ruta_del_servidor_de_clientes() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"clienteId": str(CLIENTE_ID), "estado": "VERIFICADO"})

    adaptador = _adaptador(manejador, "http://simulador-aliados:8080/clientes-stub/")
    await adaptador.obtener(CLIENTE_ID, "actor-1")

    assert recibidas[0].url.path == f"/clientes-stub/v1/clientes/{CLIENTE_ID}/identidad"


async def test_un_404_es_cliente_no_encontrado() -> None:
    adaptador = _adaptador(lambda _: httpx.Response(404))

    with pytest.raises(ClienteNoEncontrado):
        await adaptador.obtener(CLIENTE_ID, "actor-1")


async def test_un_timeout_no_se_reintenta() -> None:
    llamadas = 0

    def manejador(_: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        raise httpx.ReadTimeout("sin respuesta")

    with pytest.raises(ServicioNoDisponible) as error:
        await _adaptador(manejador).obtener(CLIENTE_ID, "actor-1")

    assert error.value.motivo == "TIEMPO_AGOTADO"
    assert llamadas == 1


async def test_un_5xx_es_servicio_no_disponible() -> None:
    with pytest.raises(ServicioNoDisponible) as error:
        await _adaptador(lambda _: httpx.Response(503)).obtener(CLIENTE_ID, "actor-1")

    assert error.value.motivo == "ERROR"
