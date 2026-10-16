import json
from collections.abc import Callable
from uuid import UUID

import httpx
import pytest
from adaptador_identidad.adapters.outbound.kyc_http import KycHttp
from adaptador_identidad.config import Settings
from adaptador_identidad.domain.verificaciones.modelos import (
    RespuestaInvalida,
    ResultadoVerificacion,
)
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
CLAVE = "clave-sintetica-de-prueba"


def _proveedor(
    manejador: Callable[[httpx.Request], httpx.Response], base: str = "http://sim/kyc"
) -> KycHttp:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    return KycHttp(cliente, base, SecretStr(CLAVE))


async def test_url_cuerpo_y_api_key() -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"resultado": "APROBADO", "motivo": None})

    verificacion = await _proveedor(manejador).verificar(
        CLIENTE, "selfie-sintetica-aprobada", {"completado": True}, 0.3
    )
    assert verificacion.resultado == ResultadoVerificacion.APROBADO
    assert verificacion.motivo is None
    (request,) = recibidas
    assert request.method == "POST"
    assert request.url.path == "/kyc/v1/verificaciones"
    assert json.loads(request.content) == {
        "clienteId": str(CLIENTE),
        "selfie": "selfie-sintetica-aprobada",
        "pruebaDeVida": {"completado": True},
    }
    assert request.headers["X-Api-Key"] == CLAVE
    assert request.extensions["timeout"]["read"] == 0.3


async def test_resultado_captura_invalida_con_motivo() -> None:
    verificacion = await _proveedor(
        lambda _: httpx.Response(200, json={"resultado": "CAPTURA_INVALIDA", "motivo": "X"})
    ).verificar(CLIENTE, "selfie", {}, 0.3)
    assert verificacion.resultado == ResultadoVerificacion.CAPTURA_INVALIDA
    assert verificacion.motivo == "X"


async def test_404_es_respuesta_invalida() -> None:
    with pytest.raises(RespuestaInvalida) as error:
        await _proveedor(lambda _: httpx.Response(404)).verificar(CLIENTE, "selfie", {}, 0.3)
    assert error.value.motivo == "HTTP_4XX"


async def test_503_lanza_http_status_error() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await _proveedor(lambda _: httpx.Response(503)).verificar(CLIENTE, "selfie", {}, 0.3)


async def test_json_invalido() -> None:
    with pytest.raises(RespuestaInvalida) as error:
        await _proveedor(lambda _: httpx.Response(200, text="<html>")).verificar(
            CLIENTE, "selfie", {}, 0.3
        )
    assert error.value.motivo == "JSON"


async def test_resultado_desconocido_es_respuesta_invalida() -> None:
    with pytest.raises(RespuestaInvalida) as error:
        await _proveedor(
            lambda _: httpx.Response(200, json={"resultado": "NO_EXISTE", "motivo": None})
        ).verificar(CLIENTE, "selfie", {}, 0.3)
    assert error.value.motivo == "JSON"


def test_credenciales_fuera_de_repr_y_logs(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., object]
) -> None:
    credenciales = json.dumps(
        {"client_id": "id-centinela-kyc", "client_secret": "secreto-centinela-kyc"}
    )
    monkeypatch.setenv("ALLY_KYC_CREDENTIALS", credenciales)
    salida = capturar_logs()
    settings = Settings()
    assert settings.ally_kyc_credentials.client_secret.get_secret_value() == "secreto-centinela-kyc"
    import logging

    logging.getLogger("prueba").info(
        "configuración", extra={"settings": repr(settings), "valor": str(settings)}
    )
    for texto in (repr(settings), str(settings), salida.getvalue()):
        assert "id-centinela-kyc" not in texto
        assert "secreto-centinela-kyc" not in texto


@pytest.mark.parametrize(
    "credenciales",
    ['{"client_id": "solo-id"}', '{"client_secret": "solo-secreto"}', "no-es-json"],
)
def test_credenciales_incompletas_fallan(
    monkeypatch: pytest.MonkeyPatch, credenciales: str
) -> None:
    monkeypatch.setenv("ALLY_KYC_CREDENTIALS", credenciales)
    with pytest.raises((ValidationError, SettingsError)) as error:
        Settings()
    assert "solo-secreto" not in str(error.value)
    assert "solo-id" not in str(error.value)
