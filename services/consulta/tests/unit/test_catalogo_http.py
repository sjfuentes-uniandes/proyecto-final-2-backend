import json
from typing import Any

import httpx
import pytest
from consulta.adapters.outbound.catalogo_http import CatalogoHttp
from consulta.application.proyeccion.puertos import DependenciaNoDisponible

pytestmark = pytest.mark.anyio

PRODUCTO = {
    "id": "vida-deudor",
    "version": "1.0.0",
    "nombre": "Vida deudor",
    "ramo": "VIDA",
    "pais": "CO",
    "moneda": "COP",
    "estado": "ACTIVO",
    "vigenteDesde": "2026-01-01T00:00:00Z",
    "vigenteHasta": None,
    "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
}


def _catalogo(respuesta: Any) -> tuple[CatalogoHttp, list[httpx.Request]]:
    vistas: list[httpx.Request] = []

    def manejar(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta  # type: ignore[no-any-return]

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejar))
    return CatalogoHttp(cliente, "http://catalogo/", 300), vistas


async def test_lee_la_version_vigente_con_ramo_y_coberturas() -> None:
    catalogo, vistas = _catalogo(httpx.Response(200, content=json.dumps(PRODUCTO)))

    producto = await catalogo.producto("vida-deudor")

    assert str(vistas[0].url) == "http://catalogo/v1/productos/vida-deudor"
    assert (producto.version, producto.nombre, producto.ramo) == ("1.0.0", "Vida deudor", "VIDA")
    assert [(c.codigo, c.obligatoria) for c in producto.coberturas] == [("MUERTE", True)]


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(404, json={"codigo": "PRODUCTO_NO_ENCONTRADO"}),
        httpx.Response(503),
        httpx.Response(200, content=b"no es json"),
        httpx.Response(200, content=json.dumps({"id": "vida-deudor"})),
        httpx.ConnectError("sin conexión"),
        httpx.ReadTimeout("lento"),
    ],
)
async def test_falla_cerrado_ante_cualquier_error(respuesta: Any) -> None:
    catalogo, _ = _catalogo(respuesta)

    with pytest.raises(DependenciaNoDisponible):
        await catalogo.producto("vida-deudor")
