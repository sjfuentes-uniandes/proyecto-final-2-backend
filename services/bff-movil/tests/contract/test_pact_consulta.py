"""Pacto consumidor `bff-movil` → proveedor `consulta`: lectura de pólizas del cliente (HU-M10).

Lo genera `make test-contract SERVICE=bff-movil` en `contracts/pact/bff-movil-consulta.json`.
Cada interacción ejercita el adaptador real (`PolizasHttp`) contra el mock de Pact.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import jsonschema
import pytest
import yaml
from bff_movil.adapters.outbound.polizas_http import PolizasHttp
from bff_movil.domain.polizas.modelos import PolizaNoAutorizada, PolizaNoEncontrada
from pact import Pact, match
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "bff-movil-consulta.json"
SUB = "22222222-2222-4222-8222-222222222222"
CLIENTE_ID = "00000000-0000-4000-8000-000000000001"
POLIZA_ID = "00000000-0000-4000-8000-0000000000b1"
AJENA_ID = "00000000-0000-4000-8000-0000000000b2"
INEXISTENTE_ID = "00000000-0000-4000-8000-0000000000b3"
RUTA = f"/v1/clientes/{CLIENTE_ID}/polizas"
FECHA = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def poliza(poliza_id: str = POLIZA_ID) -> dict[str, object]:
    return {
        "polizaId": match.regex(poliza_id, regex=UUID_RE),
        "numero": match.like("100-2026-000001"),
        "estado": match.like("ACTIVA"),
        "productoId": match.like("vida-deudor"),
        "productoVersion": match.like("1.0.0"),
        "productoNombre": match.like("Vida deudor"),
        "ramo": match.like("VIDA"),
        "vigencia": {
            "desde": match.regex("2026-10-25", regex=r"^\d{4}-\d{2}-\d{2}$"),
            "hasta": match.regex("2027-10-24", regex=r"^\d{4}-\d{2}-\d{2}$"),
        },
        "vigente": match.like(True),
        "prima": match.like("64638.10"),
        "sumaAsegurada": match.like("50000000"),
        "moneda": match.like("COP"),
        "periodicidad": match.like("MENSUAL"),
        "coberturas": match.each_like(
            {
                "codigo": match.like("MUERTE"),
                "nombre": match.like("Muerte"),
                "obligatoria": match.like(True),
            }
        ),
        "documentoHuella": match.like("sha256:" + "a" * 64),
        "firmaId": match.like("FIRMA-0001"),
        "emitidaEn": match.regex("2026-10-24T15:00:01.250000Z", regex=FECHA),
    }


def error(codigo: str) -> dict[str, object]:
    return {
        "codigo": codigo,
        "mensaje": match.like("Mensaje genérico"),
        "correlationId": match.like("c0rr3l4c10n"),
        "detalles": {},
    }


def _con_actor(interaccion):
    return interaccion.with_header("X-Actor-Id", SUB).with_header("X-Actor-Rol", "CLIENTE")


def _adaptador(url: str) -> PolizasHttp:
    return PolizasHttp(correlation.cliente_async(), url, 2000)


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    pacto = Pact("bff-movil", "consulta").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


async def test_listar(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("la lista de pólizas del cliente").with_request("GET", RUTA)
        )
        .given("el cliente tiene una póliza")
        .will_respond_with(200)
        .with_body([poliza()], content_type="application/json")
    )
    with pacto.serve() as servidor:
        polizas = await _adaptador(str(servidor.url)).listar(UUID(CLIENTE_ID), SUB)
    assert [str(p.poliza_id) for p in polizas] == [POLIZA_ID]


async def test_listar_sin_polizas(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("la lista de un cliente sin pólizas").with_request("GET", RUTA)
        )
        .given("el cliente no tiene pólizas")
        .will_respond_with(200)
        .with_body([], content_type="application/json")
    )
    with pacto.serve() as servidor:
        assert await _adaptador(str(servidor.url)).listar(UUID(CLIENTE_ID), SUB) == []


async def test_detalle(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el detalle de una póliza del cliente").with_request(
                "GET", f"{RUTA}/{POLIZA_ID}"
            )
        )
        .given("el cliente tiene una póliza")
        .will_respond_with(200)
        .with_body(poliza(), content_type="application/json")
    )
    with pacto.serve() as servidor:
        detalle = await _adaptador(str(servidor.url)).obtener(
            UUID(CLIENTE_ID), UUID(POLIZA_ID), SUB
        )
    assert str(detalle.poliza_id) == POLIZA_ID
    assert (detalle.producto_nombre, detalle.ramo) == ("Vida deudor", "VIDA")


async def test_detalle_de_otro_cliente(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el detalle de una póliza de otro cliente").with_request(
                "GET", f"{RUTA}/{AJENA_ID}"
            )
        )
        .given("la póliza pertenece a otro cliente")
        .will_respond_with(403)
        .with_body(error("POLIZA_NO_AUTORIZADA"), content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(PolizaNoAutorizada):
        await _adaptador(str(servidor.url)).obtener(UUID(CLIENTE_ID), UUID(AJENA_ID), SUB)


async def test_detalle_inexistente(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el detalle de una póliza inexistente").with_request(
                "GET", f"{RUTA}/{INEXISTENTE_ID}"
            )
        )
        .given("la póliza no existe")
        .will_respond_with(404)
        .with_body(error("POLIZA_NO_ENCONTRADA"), content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(PolizaNoEncontrada):
        await _adaptador(str(servidor.url)).obtener(UUID(CLIENTE_ID), UUID(INEXISTENTE_ID), SUB)


def test_el_pacto_cumple_el_openapi_publicado_por_consulta() -> None:
    """Cada interacción existe en `contracts/openapi/consulta.yaml` y sus ejemplos validan su
    esquema. Corre al final del módulo: lee el pacto que escribieron las pruebas anteriores."""
    openapi = yaml.safe_load(
        (PACTOS.parent / "openapi" / "consulta.yaml").read_text(encoding="utf-8")
    )
    pacto = json.loads((PACTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert len(pacto["interactions"]) == 5
    for interaccion in pacto["interactions"]:
        solicitud, respuesta = interaccion["request"], interaccion["response"]
        plantilla = solicitud["path"].replace(CLIENTE_ID, "{cliente_id}")
        plantilla = plantilla.replace(f"/{POLIZA_ID}", "/{poliza_id}")
        plantilla = plantilla.replace(f"/{AJENA_ID}", "/{poliza_id}")
        plantilla = plantilla.replace(f"/{INEXISTENTE_ID}", "/{poliza_id}")
        operacion = openapi["paths"][plantilla][solicitud["method"].lower()]
        declarada = operacion["responses"][str(respuesta["status"])]
        esquema = declarada["content"]["application/json"]["schema"]
        jsonschema.Draft202012Validator({**esquema, "components": openapi["components"]}).validate(
            respuesta["body"]["content"]
        )
