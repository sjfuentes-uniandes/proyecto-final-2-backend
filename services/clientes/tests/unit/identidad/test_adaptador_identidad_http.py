import json
from collections.abc import Callable
from datetime import date
from uuid import UUID

import httpx
import pytest
from clientes.adapters.outbound.adaptador_identidad_http import AdaptadorIdentidadHttp
from clientes.domain.identidad.modelos import DatosCliente, IdentidadNoDisponible, ResultadoKyc

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")


def _adaptador(manejador: Callable[[httpx.Request], httpx.Response]) -> AdaptadorIdentidadHttp:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    return AdaptadorIdentidadHttp(cliente, "http://adaptador-identidad:8080/", 500)


async def test_url_cuerpo_y_timeout() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})

    veredicto = await _adaptador(manejador).verificar(CLIENTE, "selfie", {"ok": True})
    assert veredicto.resultado is ResultadoKyc.APROBADO
    (request,) = recibidas
    assert request.method == "POST"
    assert str(request.url) == "http://adaptador-identidad:8080/v1/verificaciones"
    assert json.loads(request.content) == {
        "clienteId": str(CLIENTE),
        "selfie": "selfie",
        "pruebaDeVida": {"ok": True},
    }
    assert request.extensions["timeout"]["read"] == 0.5


async def test_captura_invalida_con_motivo() -> None:
    veredicto = await _adaptador(
        lambda _: httpx.Response(200, json={"resultado": "CAPTURA_INVALIDA", "motivo": "X"})
    ).verificar(CLIENTE, "selfie", {})
    assert (veredicto.resultado, veredicto.motivo) == (ResultadoKyc.CAPTURA_INVALIDA, "X")


async def test_el_timeout_configurado_no_se_recorta_a_700_ms() -> None:
    """`clientes` espera más que el proveedor KYC (3 s) para no rendirse antes que el adaptador."""
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    await AdaptadorIdentidadHttp(cliente, "http://adaptador-identidad:8080", 4000).verificar(
        CLIENTE, "selfie", {}
    )
    assert recibidas[0].extensions["timeout"]["read"] == 4.0


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [
        (httpx.Response(503), "ERROR"),
        (httpx.Response(502), "ERROR"),
        (httpx.Response(504), "TIEMPO_AGOTADO"),
        (httpx.Response(422), "ERROR"),
        (httpx.Response(200, text="<html>"), "ERROR"),
        (httpx.Response(200, json={"resultado": "NO_EXISTE"}), "ERROR"),
    ],
)
async def test_errores_recuperables(respuesta: httpx.Response, motivo: str) -> None:
    with pytest.raises(IdentidadNoDisponible) as error:
        await _adaptador(lambda _: respuesta).verificar(CLIENTE, "selfie", {})
    assert error.value.motivo == motivo


@pytest.mark.parametrize(
    ("excepcion", "motivo"),
    [(httpx.ReadTimeout("lento"), "TIEMPO_AGOTADO"), (httpx.ConnectError("caído"), "ERROR")],
)
async def test_fallas_de_red(excepcion: Exception, motivo: str) -> None:
    def manejador(request: httpx.Request) -> httpx.Response:
        raise excepcion

    with pytest.raises(IdentidadNoDisponible) as error:
        await _adaptador(manejador).verificar(CLIENTE, "selfie", {})
    assert error.value.motivo == motivo


DATOS = DatosCliente("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))
APROBADO = {
    "resultado": "APROBADO",
    "motivo": None,
    "referencia": "kyc-ref-001",
    "proveedor": "KYC",
}


async def test_documento_url_cuerpo_y_veredicto() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json=APROBADO)

    veredicto = await _adaptador(manejador).verificar_documento(CLIENTE, DATOS)

    assert (veredicto.resultado, veredicto.referencia, veredicto.proveedor) == (
        ResultadoKyc.APROBADO,
        "kyc-ref-001",
        "KYC",
    )
    (request,) = recibidas
    assert str(request.url) == "http://adaptador-identidad:8080/v1/verificaciones/documento"
    assert json.loads(request.content) == {
        "clienteId": str(CLIENTE),
        "tipoDocumento": "CC",
        "numeroDocumento": "1020456789",
        "nombreCompleto": "Natalia González Ruiz",
        "fechaNacimiento": "1990-04-18",
    }
    assert request.extensions["timeout"]["read"] == 0.5


async def test_documento_rechazado_con_motivo() -> None:
    respuesta = {**APROBADO, "resultado": "RECHAZADO", "motivo": "DATOS_NO_COINCIDEN"}
    veredicto = await _adaptador(lambda _: httpx.Response(200, json=respuesta)).verificar_documento(
        CLIENTE, DATOS
    )
    assert (veredicto.resultado, veredicto.motivo) == (ResultadoKyc.RECHAZADO, "DATOS_NO_COINCIDEN")


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [
        (httpx.Response(503), "ERROR"),
        (httpx.Response(502), "ERROR"),
        (httpx.Response(504), "TIEMPO_AGOTADO"),
        (httpx.Response(422), "ERROR"),
        (httpx.Response(200, text="<html>"), "ERROR"),
        (httpx.Response(200, json={"resultado": "NO_EXISTE"}), "ERROR"),
        (httpx.Response(200, json={"resultado": "APROBADO", "motivo": None}), "ERROR"),
    ],
)
async def test_documento_errores_recuperables(respuesta: httpx.Response, motivo: str) -> None:
    with pytest.raises(IdentidadNoDisponible) as error:
        await _adaptador(lambda _: respuesta).verificar_documento(CLIENTE, DATOS)
    assert error.value.motivo == motivo


@pytest.mark.parametrize(
    ("excepcion", "motivo"),
    [(httpx.ReadTimeout("lento"), "TIEMPO_AGOTADO"), (httpx.ConnectError("caído"), "ERROR")],
)
async def test_documento_fallas_de_red(excepcion: Exception, motivo: str) -> None:
    def manejador(request: httpx.Request) -> httpx.Response:
        raise excepcion

    with pytest.raises(IdentidadNoDisponible) as error:
        await _adaptador(manejador).verificar_documento(CLIENTE, DATOS)
    assert error.value.motivo == motivo
