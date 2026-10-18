"""Verificación documental (HU-W30): el proveedor recibe datos del documento, nunca biometría."""

import json
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any
from uuid import UUID

import httpx
import pytest
from adaptador_identidad.adapters.outbound.kyc_http import KycHttp
from adaptador_identidad.config import Settings
from adaptador_identidad.domain.verificaciones.modelos import (
    DatosDocumento,
    RespuestaInvalida,
    ResultadoDocumento,
)
from adaptador_identidad.main import crear_app
from fastapi.testclient import TestClient
from pydantic import SecretStr

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
RUTA = "/v1/verificaciones/documento"
DATOS = DatosDocumento("CC", "1020456789", "Natalia González Ruiz", date(1990, 4, 18))
APROBADO = {"resultado": "APROBADO", "motivo": None, "referencia": "kyc-ref-001"}

Manejador = Callable[[httpx.Request], httpx.Response]


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "clienteId": str(CLIENTE),
        "tipoDocumento": "CC",
        "numeroDocumento": "1020456789",
        "nombreCompleto": "Natalia González Ruiz",
        "fechaNacimiento": "1990-04-18",
    }
    cuerpo.update(cambios)
    return cuerpo


@pytest.fixture
def api() -> Iterator[Callable[[Manejador], tuple[TestClient, list[httpx.Request]]]]:
    abiertos: list[TestClient] = []

    def crear(manejador: Manejador) -> tuple[TestClient, list[httpx.Request]]:
        recibidas: list[httpx.Request] = []

        async def registrar(request: httpx.Request) -> httpx.Response:
            recibidas.append(request)
            respuesta = manejador(request)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        cliente = TestClient(crear_app(Settings(), transport=httpx.MockTransport(registrar)))
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente, recibidas

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)


def _proveedor(manejador: Manejador) -> KycHttp:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    return KycHttp(cliente, "http://sim/kyc", SecretStr("clave"))


# --- Cliente HTTP del proveedor ---


@pytest.mark.anyio
async def test_proveedor_envia_solo_datos_de_documento() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json=APROBADO)

    verificacion = await _proveedor(manejador).verificar_documento(CLIENTE, DATOS, 0.3)

    assert verificacion.resultado is ResultadoDocumento.APROBADO
    assert (verificacion.referencia, verificacion.proveedor) == ("kyc-ref-001", "KYC")
    (request,) = recibidas
    assert request.url.path == "/kyc/v1/verificaciones/documento"
    cuerpo = json.loads(request.content)
    assert cuerpo == _cuerpo()
    assert "selfie" not in cuerpo and "pruebaDeVida" not in cuerpo


@pytest.mark.anyio
async def test_proveedor_rechazado_con_motivo() -> None:
    respuesta = {"resultado": "RECHAZADO", "motivo": "DATOS_NO_COINCIDEN", "referencia": "r-2"}
    verificacion = await _proveedor(
        lambda _: httpx.Response(200, json=respuesta)
    ).verificar_documento(CLIENTE, DATOS, 0.3)
    assert verificacion.resultado is ResultadoDocumento.RECHAZADO
    assert verificacion.motivo == "DATOS_NO_COINCIDEN"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"resultado": "NO_EXISTE", "referencia": "r"}),
        httpx.Response(200, json={"resultado": "CAPTURA_INVALIDA", "referencia": "r"}),
        httpx.Response(200, json={"resultado": "APROBADO"}),
        httpx.Response(200, json={"resultado": "APROBADO", "referencia": ""}),
        httpx.Response(200, json={"resultado": "APROBADO", "referencia": 7}),
        httpx.Response(200, json=["APROBADO"]),
        httpx.Response(404),
    ],
)
async def test_proveedor_respuesta_malformada(respuesta: httpx.Response) -> None:
    with pytest.raises(RespuestaInvalida):
        await _proveedor(lambda _: respuesta).verificar_documento(CLIENTE, DATOS, 0.3)


@pytest.mark.anyio
async def test_proveedor_5xx_lanza_http_status_error() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await _proveedor(lambda _: httpx.Response(503)).verificar_documento(CLIENTE, DATOS, 0.3)


# --- API ---


def test_200_aprobado_con_referencia_y_proveedor(api: Any) -> None:
    cliente, recibidas = api(lambda _: httpx.Response(200, json=APROBADO))
    respuesta = cliente.post(RUTA, json=_cuerpo())
    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "resultado": "APROBADO",
        "motivo": None,
        "referencia": "kyc-ref-001",
        "proveedor": "KYC",
    }
    assert recibidas[0].headers["X-Api-Key"] == "local-sintetico"


def test_200_rechazado(api: Any) -> None:
    cliente, _ = api(
        lambda _: httpx.Response(
            200, json={"resultado": "RECHAZADO", "motivo": "X", "referencia": "r-2"}
        )
    )
    assert cliente.post(RUTA, json=_cuerpo()).json()["resultado"] == "RECHAZADO"


@pytest.mark.parametrize(
    "cambios",
    [
        {"clienteId": "no-uuid"},
        {"tipoDocumento": ""},
        {"numeroDocumento": "1"},
        {"nombreCompleto": ""},
        {"fechaNacimiento": "18/04/1990"},
        {"fechaNacimiento": None},
        {"fechaNacimiento": 0},
    ],
)
def test_422_solicitud_invalida_no_llama_al_proveedor(api: Any, cambios: dict[str, Any]) -> None:
    cliente, recibidas = api(lambda _: httpx.Response(200, json=APROBADO))
    assert cliente.post(RUTA, json=_cuerpo(**cambios)).status_code == 422
    assert recibidas == []


@pytest.mark.parametrize(
    ("manejador", "estado", "codigo"),
    [
        (lambda r: httpx.ReadTimeout("lento", request=r), 504, "TIEMPO_AGOTADO"),
        (lambda r: httpx.ConnectError("caído", request=r), 503, "FUENTE_NO_DISPONIBLE"),
        (lambda _: httpx.Response(503), 503, "FUENTE_NO_DISPONIBLE"),
        (lambda _: httpx.Response(200, text="<html>"), 502, "RESPUESTA_INVALIDA"),
    ],
)
def test_fallas_tecnicas_tienen_codigo_propio(
    api: Any, manejador: Manejador, estado: int, codigo: str
) -> None:
    cliente, _ = api(manejador)
    respuesta = cliente.post(RUTA, json=_cuerpo())
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
