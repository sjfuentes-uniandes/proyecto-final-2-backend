"""Pacto consumidor `bff-movil` → proveedor `avisos`: registro y baja de dispositivos (HU-M11).

Lo genera `make test-contract SERVICE=bff-movil` en `contracts/pact/bff-movil-avisos.json`.
Cada interacción ejercita el adaptador real (`DispositivosHttp`) contra el mock de Pact.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import jsonschema
import pytest
import yaml
from bff_movil.adapters.outbound.dispositivos_http import DispositivosHttp
from bff_movil.domain.dispositivos.modelos import DispositivoNoAutorizado, DispositivoNoEncontrado
from pact import Pact, match
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "bff-movil-avisos.json"
SUB = "22222222-2222-4222-8222-222222222222"
CLIENTE_ID = "00000000-0000-4000-8000-000000000001"
DISPOSITIVO_ID = "00000000-0000-4000-8000-0000000000d1"
AJENO_ID = "00000000-0000-4000-8000-0000000000d2"
INEXISTENTE_ID = "00000000-0000-4000-8000-0000000000d3"
TOKEN = "token-sintetico-entregado"
RUTA = f"/v1/clientes/{CLIENTE_ID}/dispositivos"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def error(codigo: str) -> dict[str, object]:
    return {
        "codigo": codigo,
        "mensaje": match.like("Mensaje genérico"),
        "correlationId": match.like("c0rr3l4c10n"),
        "detalles": {},
    }


def _con_actor(interaccion):
    return interaccion.with_header("X-Actor-Id", SUB).with_header("X-Actor-Rol", "CLIENTE")


def _adaptador(url: str) -> DispositivosHttp:
    return DispositivosHttp(correlation.cliente_async(), url, 2000)


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    pacto = Pact("bff-movil", "avisos").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


async def test_registrar(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el registro de un dispositivo del cliente").with_request(
                "POST", RUTA
            )
        )
        .with_body({"token": TOKEN}, content_type="application/json")
        .given("el cliente puede registrar dispositivos")
        .will_respond_with(201)
        .with_body(
            {"dispositivoId": match.regex(DISPOSITIVO_ID, regex=UUID_RE)},
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        resultado = await _adaptador(str(servidor.url)).registrar(UUID(CLIENTE_ID), TOKEN, SUB)
    assert str(resultado) == DISPOSITIVO_ID


async def test_baja_propia(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("la baja de un dispositivo del cliente").with_request(
                "DELETE", f"{RUTA}/{DISPOSITIVO_ID}"
            )
        )
        .given("el dispositivo es del cliente")
        .will_respond_with(204)
    )
    with pacto.serve() as servidor:
        await _adaptador(str(servidor.url)).dar_de_baja(UUID(CLIENTE_ID), UUID(DISPOSITIVO_ID), SUB)


async def test_baja_de_otro_cliente(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("la baja de un dispositivo de otro cliente").with_request(
                "DELETE", f"{RUTA}/{AJENO_ID}"
            )
        )
        .given("el dispositivo pertenece a otro cliente")
        .will_respond_with(403)
        .with_body(error("DISPOSITIVO_NO_AUTORIZADO"), content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(DispositivoNoAutorizado):
        await _adaptador(str(servidor.url)).dar_de_baja(UUID(CLIENTE_ID), UUID(AJENO_ID), SUB)


async def test_baja_inexistente(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("la baja de un dispositivo inexistente").with_request(
                "DELETE", f"{RUTA}/{INEXISTENTE_ID}"
            )
        )
        .given("el dispositivo no existe")
        .will_respond_with(404)
        .with_body(error("DISPOSITIVO_NO_ENCONTRADO"), content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(DispositivoNoEncontrado):
        await _adaptador(str(servidor.url)).dar_de_baja(UUID(CLIENTE_ID), UUID(INEXISTENTE_ID), SUB)


def test_el_pacto_cumple_el_openapi_publicado_por_avisos() -> None:
    """Cada interacción existe en `contracts/openapi/avisos.yaml` y sus ejemplos validan su
    esquema. Corre al final del módulo: lee el pacto que escribieron las pruebas anteriores."""
    openapi = yaml.safe_load(
        (PACTOS.parent / "openapi" / "avisos.yaml").read_text(encoding="utf-8")
    )
    pacto = json.loads((PACTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert len(pacto["interactions"]) == 4
    for interaccion in pacto["interactions"]:
        solicitud, respuesta = interaccion["request"], interaccion["response"]
        plantilla = solicitud["path"].replace(CLIENTE_ID, "{cliente_id}")
        for dispositivo in (DISPOSITIVO_ID, AJENO_ID, INEXISTENTE_ID):
            plantilla = plantilla.replace(f"/{dispositivo}", "/{dispositivo_id}")
        operacion = openapi["paths"][plantilla][solicitud["method"].lower()]
        declarada = operacion["responses"][str(respuesta["status"])]
        validador = jsonschema.Draft202012Validator(
            {**operacion_esquema(operacion, "requestBody"), "components": openapi["components"]}
        )
        if solicitud["method"] == "POST":
            validador.validate(solicitud["body"]["content"])
        if "content" in declarada:
            esquema = declarada["content"]["application/json"]["schema"]
            jsonschema.Draft202012Validator(
                {**esquema, "components": openapi["components"]}
            ).validate(respuesta["body"]["content"])
        else:
            assert "body" not in respuesta


def operacion_esquema(operacion: dict[str, object], clave: str) -> dict[str, object]:
    cuerpo = operacion.get(clave)
    if not cuerpo:
        return {}
    return cuerpo["content"]["application/json"]["schema"]  # type: ignore[index,return-value]
