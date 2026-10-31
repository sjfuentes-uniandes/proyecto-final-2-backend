"""EnviadorPush HTTP hacia adaptador-firma: falla cerrado sin URL y traduce cada respuesta."""

import json
from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest
import respx
from avisos.adapters.outbound.push_http import EnviadorPushHttp
from avisos.application.entregas.puertos import FalloTransitorio
from avisos.domain.avisos.modelos import (
    FALLO_ADAPTADOR_FIRMA,
    FALLO_SIN_CONFIGURAR,
    FALLO_TIEMPO_AGOTADO,
    Aviso,
)

pytestmark = pytest.mark.anyio

BASE = "http://adaptador-firma:8080"
POLIZA = UUID("5b1c9e2a-7f3d-4a60-8c11-000000000d01")
EVENTO = UUID("00000000-0000-4000-8000-000000000e01")
AVISO = Aviso(tipo="POLIZA_EMITIDA", poliza_id=POLIZA, event_id=EVENTO)
TOKEN = "token-sintetico-entregado"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _enviador(base_url: str | None = BASE, timeout_ms: int = 3000) -> EnviadorPushHttp:
    return EnviadorPushHttp(httpx.AsyncClient(), base_url, timeout_ms)


async def test_sin_url_falla_cerrado_sin_llamar_a_nadie(mock: respx.MockRouter) -> None:
    with pytest.raises(FalloTransitorio) as error:
        await _enviador(base_url=None).enviar(TOKEN, AVISO)
    assert error.value.codigo == FALLO_SIN_CONFIGURAR
    assert mock.calls.call_count == 0


async def test_envia_el_cuerpo_exacto(mock: respx.MockRouter) -> None:
    ruta = mock.post("/v1/notificaciones").respond(200, json={"resultado": "OK", "envioId": "P-1"})
    assert await _enviador().enviar(TOKEN, AVISO) is True
    request = ruta.calls.last.request
    assert json.loads(request.content) == {
        "tokenDispositivo": TOKEN,
        "tipo": "POLIZA_EMITIDA",
        "polizaId": str(POLIZA),
        "eventId": str(EVENTO),
    }


async def test_rechazado_devuelve_false() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/v1/notificaciones").respond(200, json={"resultado": "RECHAZADO"})
        assert await _enviador().enviar(TOKEN, AVISO) is False


async def test_timeout_es_fallo_transitorio() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/v1/notificaciones").mock(side_effect=httpx.ReadTimeout("lento"))
        with pytest.raises(FalloTransitorio) as error:
            await _enviador(timeout_ms=50).enviar(TOKEN, AVISO)
        assert error.value.codigo == FALLO_TIEMPO_AGOTADO


@pytest.mark.parametrize("estado", [500, 502, 503, 504])
async def test_5xx_es_fallo_transitorio(estado: int) -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/v1/notificaciones").respond(estado, json={"codigo": "ERROR"})
        with pytest.raises(FalloTransitorio) as error:
            await _enviador().enviar(TOKEN, AVISO)
        assert error.value.codigo == FALLO_ADAPTADOR_FIRMA


@pytest.mark.parametrize(
    "cuerpo",
    [
        {},
        {"resultado": "OTRO"},
        {"envioId": "P-1"},
    ],
)
async def test_cuerpo_fuera_de_forma_es_fallo_transitorio(cuerpo: dict[str, str]) -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/v1/notificaciones").respond(200, json=cuerpo)
        with pytest.raises(FalloTransitorio) as error:
            await _enviador().enviar(TOKEN, AVISO)
        assert error.value.codigo == FALLO_ADAPTADOR_FIRMA


async def test_cuerpo_no_json_es_fallo_transitorio() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/v1/notificaciones").respond(200, text="no soy json")
        with pytest.raises(FalloTransitorio) as error:
            await _enviador().enviar(TOKEN, AVISO)
        assert error.value.codigo == FALLO_ADAPTADOR_FIRMA


@pytest.mark.parametrize("estado", [400, 409, 422])
async def test_otro_estado_inesperado_es_fallo_transitorio(estado: int) -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/v1/notificaciones").respond(estado, json={"codigo": "X"})
        with pytest.raises(FalloTransitorio) as error:
            await _enviador().enviar(TOKEN, AVISO)
        assert error.value.codigo == FALLO_ADAPTADOR_FIRMA
