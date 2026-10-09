import json
from collections.abc import Callable
from uuid import UUID

import httpx
import pytest
from adaptador_datos.adapters.outbound.datos_abiertos_http import DatosAbiertosHttp
from adaptador_datos.adapters.outbound.open_finance_http import OpenFinanceHttp
from adaptador_datos.config import Settings
from adaptador_datos.domain.consultas.modelos import Fuente, RespuestaInvalida
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError

pytestmark = pytest.mark.anyio

CLIENTE = UUID("00000000-0000-4000-8000-000000000001")
CLAVE = "clave-sintetica-de-prueba"


def _proveedor(
    clase: type[OpenFinanceHttp] | type[DatosAbiertosHttp],
    manejador: Callable[[httpx.Request], httpx.Response],
    base: str,
) -> OpenFinanceHttp | DatosAbiertosHttp:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    return clase(cliente, base, SecretStr(CLAVE))


@pytest.mark.parametrize(
    ("clase", "base", "fuente", "claves", "ruta", "params"),
    [
        (
            OpenFinanceHttp,
            "http://sim/open-finance/",
            Fuente.FA_PRODUCTOS_VIGENTES,
            ("creditorCount", "oldestProductYears", "totalMonthlyInstallment"),
            f"/open-finance/v3/customers/{CLIENTE}/active-products",
            {"fields": "creditorCount,oldestProductYears,totalMonthlyInstallment"},
        ),
        (
            OpenFinanceHttp,
            "http://sim/open-finance",
            Fuente.FA_HISTORIAL_PAGOS_12M,
            ("lateCount",),
            f"/open-finance/v3/customers/{CLIENTE}/payment-history",
            {"months": "12", "fields": "lateCount"},
        ),
        (
            OpenFinanceHttp,
            "http://sim/open-finance",
            Fuente.FA_INGRESOS_AGREGADOS,
            ("estimatedMonthlyIncome",),
            f"/open-finance/v3/customers/{CLIENTE}/aggregated-income",
            {"fields": "estimatedMonthlyIncome"},
        ),
        (
            DatosAbiertosHttp,
            "http://sim/datos-abiertos",
            Fuente.DA_REGISTROS_PUBLICOS,
            ("listasRestrictivas",),
            f"/datos-abiertos/v1/registros/{CLIENTE}",
            {"campos": "listasRestrictivas"},
        ),
    ],
)
async def test_ut_w10_13_url_parametros_y_api_key(
    clase: type[OpenFinanceHttp],
    base: str,
    fuente: Fuente,
    claves: tuple[str, ...],
    ruta: str,
    params: dict[str, str],
) -> None:
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return httpx.Response(200, json={"ok": True})

    assert await _proveedor(clase, manejador, base).obtener(fuente, CLIENTE, claves, 0.3) == {
        "ok": True
    }
    (request,) = recibidas
    assert request.method == "GET"
    assert request.url.path == ruta
    assert dict(request.url.params) == params
    assert request.headers["X-Api-Key"] == CLAVE
    assert request.extensions["timeout"]["read"] == 0.3


async def test_ut_w10_14_404_es_respuesta_invalida() -> None:
    proveedor = _proveedor(OpenFinanceHttp, lambda _: httpx.Response(404), "http://sim")
    with pytest.raises(RespuestaInvalida) as error:
        await proveedor.obtener(Fuente.FA_HISTORIAL_PAGOS_12M, CLIENTE, ("lateCount",), 0.3)
    assert error.value.motivo == "HTTP_4XX"


async def test_ut_w10_14_503_lanza_http_status_error() -> None:
    proveedor = _proveedor(OpenFinanceHttp, lambda _: httpx.Response(503), "http://sim")
    with pytest.raises(httpx.HTTPStatusError):
        await proveedor.obtener(Fuente.FA_HISTORIAL_PAGOS_12M, CLIENTE, ("lateCount",), 0.3)


async def test_ut_w10_14_json_invalido() -> None:
    proveedor = _proveedor(
        OpenFinanceHttp, lambda _: httpx.Response(200, text="<html>"), "http://sim"
    )
    with pytest.raises(RespuestaInvalida) as error:
        await proveedor.obtener(Fuente.FA_HISTORIAL_PAGOS_12M, CLIENTE, ("lateCount",), 0.3)
    assert error.value.motivo == "JSON"


async def test_fuente_de_otro_aliado_se_rechaza() -> None:
    proveedor = _proveedor(DatosAbiertosHttp, lambda _: httpx.Response(200, json={}), "http://sim")
    with pytest.raises(ValueError):
        await proveedor.obtener(Fuente.FA_HISTORIAL_PAGOS_12M, CLIENTE, ("lateCount",), 0.3)


def test_ut_w10_15_credenciales_fuera_de_repr_y_logs(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., object]
) -> None:
    credenciales = json.dumps(
        {"client_id": "id-centinela-15", "client_secret": "secreto-centinela-15"}
    )
    monkeypatch.setenv("ALLY_OPEN_FINANCE_CREDENTIALS", credenciales)
    monkeypatch.setenv("SOLVENTA_ALLY_DATOS_ABIERTOS_CREDENTIALS", credenciales)
    salida = capturar_logs()
    settings = Settings()
    assert (
        settings.ally_open_finance_credentials.client_secret.get_secret_value()
        == "secreto-centinela-15"
    )
    assert (
        settings.ally_datos_abiertos_credentials.client_id.get_secret_value() == "id-centinela-15"
    )
    import logging

    logging.getLogger("prueba").info(
        "configuración", extra={"settings": repr(settings), "valor": str(settings)}
    )
    for texto in (
        repr(settings),
        str(settings),
        salida.getvalue(),
        repr(settings.ally_open_finance_credentials),
    ):  # type: ignore[attr-defined]
        assert "id-centinela-15" not in texto
        assert "secreto-centinela-15" not in texto


@pytest.mark.parametrize(
    "credenciales",
    ['{"client_id": "solo-id"}', '{"client_secret": "solo-secreto"}', "no-es-json"],
)
def test_ut_w10_15_credenciales_incompletas_fallan(
    monkeypatch: pytest.MonkeyPatch, credenciales: str
) -> None:
    monkeypatch.setenv("ALLY_OPEN_FINANCE_CREDENTIALS", credenciales)
    with pytest.raises((ValidationError, SettingsError)) as error:
        Settings()
    assert "solo-secreto" not in str(error.value)
    assert "solo-id" not in str(error.value)
