"""ClientesCliente reutiliza un httpx.AsyncClient por event loop y URL (conexiones persistentes)."""

import asyncio
from typing import Any
from uuid import UUID

import httpx
import pytest
from bff_web.adapters.outbound import clientes as modulo
from bff_web.adapters.outbound.clientes import ClientesCliente, cerrar_compartidos

C01 = UUID("00000000-0000-4000-8000-000000000001")
C02 = UUID("00000000-0000-4000-8000-000000000002")


@pytest.fixture
def creados(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Sustituye cliente_async por uno con MockTransport y anota cada cliente creado."""
    lista: list[httpx.AsyncClient] = []

    def responder(solicitud: httpx.Request) -> httpx.Response:
        cliente_id = solicitud.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"clienteId": cliente_id, "nombreCompleto": "Cliente"})

    def crear(**opciones: Any) -> httpx.AsyncClient:
        cliente = httpx.AsyncClient(transport=httpx.MockTransport(responder), **opciones)
        lista.append(cliente)
        return cliente

    monkeypatch.setattr(modulo, "cliente_async", crear)
    return lista


def test_las_lecturas_del_mismo_loop_reutilizan_el_cliente(
    creados: list[httpx.AsyncClient],
) -> None:
    async def leer() -> list[dict[str, Any]]:
        servicio = ClientesCliente("http://clientes:8080/", 500)
        otro = ClientesCliente("http://clientes:8080", 500)
        resultados = await asyncio.gather(
            servicio.obtener(C01, "asesor"),
            otro.obtener(C02, "asesor"),
            servicio.obtener(C02, "asesor"),
        )
        await cerrar_compartidos()
        return list(resultados)

    resultados = asyncio.run(leer())
    assert [r["clienteId"] for r in resultados] == [str(C01), str(C02), str(C02)]
    assert len(creados) == 1
    assert creados[0].is_closed


def test_cada_loop_tiene_su_cliente(creados: list[httpx.AsyncClient]) -> None:
    async def leer() -> None:
        await ClientesCliente("http://clientes:8080", 500).obtener(C01, "asesor")

    asyncio.run(leer())
    asyncio.run(leer())
    assert len(creados) == 2


def test_un_cliente_cerrado_se_reemplaza(creados: list[httpx.AsyncClient]) -> None:
    async def leer() -> None:
        servicio = ClientesCliente("http://clientes:8080", 500)
        await servicio.obtener(C01, "asesor")
        await creados[0].aclose()
        await servicio.obtener(C01, "asesor")
        await cerrar_compartidos()

    asyncio.run(leer())
    assert len(creados) == 2


def test_el_timeout_de_la_llamada_es_el_del_cliente(creados: list[httpx.AsyncClient]) -> None:
    vistos: list[float | None] = []

    async def leer() -> None:
        await ClientesCliente("http://clientes:8080", 750).obtener(C01, "asesor")
        await cerrar_compartidos()

    original = httpx.AsyncClient.request

    async def espiar(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> httpx.Response:
        vistos.append(kwargs.get("timeout"))
        return await original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as parche:
        parche.setattr(httpx.AsyncClient, "request", espiar)
        asyncio.run(leer())
    assert vistos == [0.75]
