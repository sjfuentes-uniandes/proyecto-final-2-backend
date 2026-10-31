"""CT-M11: el cuerpo que `EnviadorPushHttp` envía a `adaptador-firma` y las respuestas que
interpreta (OK, RECHAZADO, 503, 504) cumplen `contracts/openapi/adaptador-firma.yaml`.

Valida la forma, no el comportamiento del adaptador (cubierto en `tests/unit/test_push_http.py`):
aquí el contrato es la fuente de verdad, para detectar si el publicado por adaptador-firma se
desalinea con lo que avisos arma y lee.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
import yaml
from avisos.adapters.outbound.push_http import EnviadorPushHttp
from avisos.application.entregas.puertos import FalloTransitorio
from avisos.domain.avisos.modelos import FALLO_ADAPTADOR_FIRMA, Aviso
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.anyio

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "adaptador-firma.yaml"
BASE = "http://adaptador-firma:8080"
POLIZA = UUID("5b1c9e2a-7f3d-4a60-8c11-000000000d01")
EVENTO = UUID("00000000-0000-4000-8000-000000000e01")
AVISO = Aviso(tipo="POLIZA_EMITIDA", poliza_id=POLIZA, event_id=EVENTO)
TOKEN = "token-sintetico-entregado"


def _documento() -> dict[str, Any]:
    return yaml.safe_load(CONTRATO.read_text(encoding="utf-8"))


def _esquema(nombre: str) -> dict[str, Any]:
    """Inlinea los `$ref` locales (`#/components/schemas/X`): los esquemas aquí no se anidan más
    de un nivel, así que basta con una sustitución recursiva simple."""
    esquemas = _documento()["components"]["schemas"]

    def _en(nodo: Any) -> Any:
        if isinstance(nodo, dict):
            if set(nodo) == {"$ref"}:
                return _en(esquemas[nodo["$ref"].removeprefix("#/components/schemas/")])
            return {clave: _en(valor) for clave, valor in nodo.items()}
        if isinstance(nodo, list):
            return [_en(valor) for valor in nodo]
        return nodo

    return _en(esquemas[nombre])


SOLICITUD = Draft202012Validator(_esquema("SolicitudPush"))
RESPUESTA = Draft202012Validator(_esquema("PushDto"))
ERROR = Draft202012Validator(_esquema("ErrorDto"))


def _error_dto(codigo: str) -> dict[str, Any]:
    # La forma exacta que adaptador-firma publica para sus errores (ErrorDto).
    return {"codigo": codigo, "mensaje": "x", "correlationId": None, "detalles": {}}


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _enviador() -> EnviadorPushHttp:
    return EnviadorPushHttp(httpx.AsyncClient(), BASE, timeout_ms=3000)


def test_el_path_del_contrato_coincide() -> None:
    assert "/v1/notificaciones" in _documento()["paths"]


async def test_el_cuerpo_enviado_cumple_solicitudPush(mock: respx.MockRouter) -> None:
    ruta = mock.post("/v1/notificaciones").respond(200, json={"resultado": "OK"})
    await _enviador().enviar(TOKEN, AVISO)
    cuerpo = json.loads(ruta.calls.last.request.content)
    assert SOLICITUD.is_valid(cuerpo)
    assert not list(SOLICITUD.iter_errors(cuerpo))


async def test_la_respuesta_ok_cumple_pushDto_y_se_acepta(mock: respx.MockRouter) -> None:
    cuerpo = {"resultado": "OK", "envioId": "PUSH-SINT-0001"}
    mock.post("/v1/notificaciones").respond(200, json=cuerpo)
    assert RESPUESTA.is_valid(cuerpo)
    assert await _enviador().enviar(TOKEN, AVISO) is True


async def test_la_respuesta_rechazada_cumple_pushDto_y_no_se_acepta(
    mock: respx.MockRouter,
) -> None:
    cuerpo = {"resultado": "RECHAZADO"}
    mock.post("/v1/notificaciones").respond(200, json=cuerpo)
    assert RESPUESTA.is_valid(cuerpo)
    assert await _enviador().enviar(TOKEN, AVISO) is False


@pytest.mark.parametrize(
    ("estado", "codigo"),
    [(503, "FUENTE_NO_DISPONIBLE"), (504, "TIEMPO_AGOTADO")],
)
async def test_los_errores_5xx_cumplen_errorDto_y_son_fallo_transitorio(
    mock: respx.MockRouter, estado: int, codigo: str
) -> None:
    cuerpo = _error_dto(codigo)
    mock.post("/v1/notificaciones").respond(estado, json=cuerpo)
    assert ERROR.is_valid(cuerpo)
    with pytest.raises(FalloTransitorio) as error:
        await _enviador().enviar(TOKEN, AVISO)
    # adaptador-firma ya tradujo el fallo de su proveedor; avisos solo lo reintenta sin
    # distinguir 503 de 504 (ambos cuentan igual para su propio barrido).
    assert error.value.codigo == FALLO_ADAPTADOR_FIRMA
