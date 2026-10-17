"""Adaptador de consentimientos hacia `clientes`: rutas y saneamiento de lo que se reenvía."""

from uuid import UUID

import httpx
import pytest
from bff_movil.adapters.outbound.consentimientos_http import MAX_CAMPOS, ConsentimientosHttp
from bff_movil.domain.consentimientos.modelos import SolicitudOtorgamiento, SolicitudRechazada
from solventa_common import correlation

pytestmark = pytest.mark.anyio

CLAVE = UUID("6f1c2b9e-3d4a-4e8f-9b7c-1a2b3c4d5e6f")
VACIA = SolicitudOtorgamiento(None, None, None, None, None)


def _adaptador(manejador, base_url: str = "http://clientes.test") -> ConsentimientosHttp:
    cliente = correlation.cliente_async(transport=httpx.MockTransport(manejador))
    return ConsentimientosHttp(cliente, base_url, 500, 3000)


def _rechazo(detalles: object) -> httpx.Response:
    return httpx.Response(
        422,
        json={
            "codigo": "SOLICITUD_INVALIDA",
            "mensaje": "x",
            "correlationId": None,
            "detalles": detalles,
        },
    )


async def test_respeta_el_prefijo_de_ruta_del_servidor_de_clientes() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json=[])

    await _adaptador(manejador, "http://simulador:8080/clientes-stub/").listar("sub-1")

    assert recibidas[0].url.path == "/clientes-stub/v1/clientes/actual/consentimientos"


async def test_un_cuerpo_vacio_se_envia_con_todos_los_campos_nulos() -> None:
    """`clientes` informa todos los faltantes (AC3): el BFF no los omite ni los completa."""
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return _rechazo({"campos": ["proposito"]})

    with pytest.raises(SolicitudRechazada):
        await _adaptador(manejador).otorgar("sub-1", CLAVE, VACIA)

    assert recibidas[0].read() == (
        b'{"proposito":null,"alcances":null,"vigenciaMeses":null,"versionTexto":null,"idioma":null}'
    )


@pytest.mark.parametrize(
    ("detalles", "esperados"),
    [
        ({"campos": ["proposito", 3, {"valor": "x"}, "idioma"]}, ("proposito", "idioma")),
        ({"campos": "proposito"}, ()),
        ({}, ()),
        ("no-es-objeto", ()),
    ],
)
async def test_solo_se_reenvian_rutas_de_campos_nunca_otros_valores(
    detalles: object, esperados: tuple[str, ...]
) -> None:
    with pytest.raises(SolicitudRechazada) as falla:
        await _adaptador(lambda _: _rechazo(detalles)).otorgar("sub-1", CLAVE, VACIA)

    assert falla.value.codigo == "SOLICITUD_INVALIDA"
    assert falla.value.campos == esperados


async def test_las_rutas_de_campos_se_acotan() -> None:
    campos = [f"campo{i}" for i in range(MAX_CAMPOS + 10)]

    with pytest.raises(SolicitudRechazada) as falla:
        await _adaptador(lambda _: _rechazo({"campos": campos})).otorgar("sub-1", CLAVE, VACIA)

    assert len(falla.value.campos) == MAX_CAMPOS
