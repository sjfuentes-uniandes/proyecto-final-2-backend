"""Pacto consumidor `bff-web` → proveedor `consulta`: estado y listado de pólizas del asesor (HU-W18).

Lo genera `make test-contract SERVICE=bff-web` en `contracts/pact/bff-web-consulta.json`. Cada
interacción ejercita el cliente real (`ConsultaCliente`) contra el mock de Pact, con `X-Actor-Id` y
`X-Actor-Rol=ASESOR`. Los 401, 403 y 5xx de consulta no van en el pacto: para el BFF son siempre
503 (unitarias y de módulo).
"""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import jsonschema
import pytest
import yaml
from bff_web.adapters.outbound.consulta import ConsultaCliente, ErrorConsulta
from pact import Pact, match

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "bff-web-consulta.json"
SUB = "sub-asesora"
POLIZA_ID = "00000000-0000-4000-8000-0000000000b1"
INEXISTENTE_ID = "00000000-0000-4000-8000-0000000000b3"
RUTA = "/v1/polizas"
FECHA = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
DIA = r"^\d{4}-\d{2}-\d{2}$"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
ESTADO = r"^(NO_INICIADA|VIGENTE|POR_VENCER|VENCIDA)$"
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def _estado() -> object:
    return match.regex("VIGENTE", regex=ESTADO)


def estado_poliza() -> dict[str, object]:
    return {
        "polizaId": match.regex(POLIZA_ID, regex=UUID_RE),
        "clienteId": match.regex("00000000-0000-4000-8000-0000000000c1", regex=UUID_RE),
        "numero": match.regex("100-2026-000001", regex=r"^[0-9]{3}-[0-9]{4}-[0-9]{6}$"),
        "estado": _estado(),
        "fechaActualizacion": match.regex("2026-10-24T15:00:05.250000Z", regex=FECHA),
        "producto": match.like("Vida deudor"),
        "ramo": match.like("Vida"),
        "vigencia": {
            "desde": match.regex("2026-10-25", regex=DIA),
            "hasta": match.regex("2027-10-24", regex=DIA),
        },
        "prima": match.like("64638.10"),
        "sumaAsegurada": match.like("50000000"),
        "moneda": match.like("COP"),
        "periodicidad": match.like("MENSUAL"),
        "emitidaEn": match.regex("2026-10-24T15:00:03.500000Z", regex=FECHA),
        "coberturas": match.each_like(
            {
                "codigo": match.like("MUERTE"),
                "nombre": match.like("Muerte"),
                "obligatoria": match.like(True),
            }
        ),
    }


def resumen() -> dict[str, object]:
    return {
        "polizaId": match.regex(POLIZA_ID, regex=UUID_RE),
        "numero": match.like("100-2026-000001"),
        "ramo": match.like("Vida"),
        "producto": match.like("Vida deudor"),
        "prima": match.like("64638.10"),
        "moneda": match.like("COP"),
        "vigenciaHasta": match.regex("2027-10-24", regex=DIA),
        "estado": _estado(),
        "fechaActualizacion": match.regex("2026-10-24T15:00:05.250000Z", regex=FECHA),
    }


def error(codigo: str) -> dict[str, object]:
    return {
        "codigo": codigo,
        "mensaje": match.like("Mensaje genérico"),
        "correlationId": match.like("c0rr3l4c10n"),
        "detalles": {},
    }


def _con_actor(interaccion):
    return interaccion.with_header("X-Actor-Id", SUB).with_header("X-Actor-Rol", "ASESOR")


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    pacto = Pact("bff-web", "consulta").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


async def test_estado_de_una_poliza(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el estado de una póliza proyectada").with_request(
                "GET", f"{RUTA}/{POLIZA_ID}/estado"
            )
        )
        .given("existe una póliza proyectada")
        .will_respond_with(200)
        .with_body(estado_poliza(), content_type="application/json")
    )
    with pacto.serve() as servidor:
        cuerpo = await ConsultaCliente(str(servidor.url), 2000).estado(UUID(POLIZA_ID), SUB)
    assert (cuerpo["polizaId"], cuerpo["estado"]) == (POLIZA_ID, "VIGENTE")


async def test_estado_de_una_poliza_inexistente(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el estado de una póliza inexistente").with_request(
                "GET", f"{RUTA}/{INEXISTENTE_ID}/estado"
            )
        )
        .given("la póliza no existe")
        .will_respond_with(404)
        .with_body(error("POLIZA_NO_ENCONTRADA"), content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(ErrorConsulta) as capturado:
        await ConsultaCliente(str(servidor.url), 2000).estado(UUID(INEXISTENTE_ID), SUB)
    assert (capturado.value.status, capturado.value.cuerpo["codigo"]) == (
        404,
        "POLIZA_NO_ENCONTRADA",
    )


async def test_listado_con_filtros(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el listado de pólizas con filtros y paginación")
            .with_request("GET", RUTA)
            .with_query_parameters(
                {
                    "ramo": "Vida",
                    "estado": "VIGENTE",
                    "vigenciaDesde": "2026-01-01",
                    "pagina": "1",
                    "tamano": "20",
                }
            )
        )
        .given("hay pólizas proyectadas")
        .will_respond_with(200)
        .with_body(
            {
                "items": match.each_like(resumen()),
                "total": match.like(1),
                "pagina": match.like(1),
                "tamano": match.like(20),
                "actualizadoEn": match.regex("2026-10-24T15:00:05.250000Z", regex=FECHA),
            },
            content_type="application/json",
        )
    )
    parametros = {
        "ramo": "Vida",
        "estado": "VIGENTE",
        "vigenciaDesde": "2026-01-01",
        "pagina": 1,
        "tamano": 20,
    }
    with pacto.serve() as servidor:
        pagina = await ConsultaCliente(str(servidor.url), 2000).listado(parametros, SUB)
    assert [i["polizaId"] for i in pagina["items"]] == [POLIZA_ID]


async def test_listado_vacio(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("el listado de pólizas sin proyecciones")
            .with_request("GET", RUTA)
            .with_query_parameters({"pagina": "1", "tamano": "20"})
        )
        .given("no hay pólizas proyectadas")
        .will_respond_with(200)
        .with_body(
            {
                "items": [],
                "total": 0,
                "pagina": match.like(1),
                "tamano": match.like(20),
                "actualizadoEn": None,
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        pagina = await ConsultaCliente(str(servidor.url), 2000).listado(
            {"pagina": 1, "tamano": 20}, SUB
        )
    assert (pagina["items"], pagina["actualizadoEn"]) == ([], None)


def test_el_pacto_cumple_el_openapi_publicado_por_consulta() -> None:
    """Cada interacción existe en `contracts/openapi/consulta.yaml` y sus ejemplos validan su
    esquema. Corre al final del módulo: lee el pacto que escribieron las pruebas anteriores."""
    openapi = yaml.safe_load(
        (PACTOS.parent / "openapi" / "consulta.yaml").read_text(encoding="utf-8")
    )
    pacto = json.loads((PACTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert len(pacto["interactions"]) == 4
    for interaccion in pacto["interactions"]:
        solicitud, respuesta = interaccion["request"], interaccion["response"]
        plantilla = solicitud["path"].replace(f"/{POLIZA_ID}/", "/{poliza_id}/")
        plantilla = plantilla.replace(f"/{INEXISTENTE_ID}/", "/{poliza_id}/")
        operacion = openapi["paths"][plantilla][solicitud["method"].lower()]
        declarados = {p["name"] for p in operacion["parameters"] if p["in"] == "query"}
        assert set(solicitud.get("query", {})) <= declarados
        declarada = operacion["responses"][str(respuesta["status"])]
        esquema = declarada["content"]["application/json"]["schema"]
        jsonschema.Draft202012Validator({**esquema, "components": openapi["components"]}).validate(
            respuesta["body"]["content"]
        )
