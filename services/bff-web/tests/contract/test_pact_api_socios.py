from collections.abc import Iterator
from pathlib import Path

import pytest
from bff_web.adapters.outbound.api_socios import ApiSocios, ErrorApiSocios
from pact import Pact, match

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ID = match.regex("SOC-000001", regex=r"^SOC-\d{6}$")
FECHA = match.regex("2026-10-12T10:00:00Z", regex=r"^\d{4}-\d{2}-\d{2}T.+$")
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


ALTA = {"nombre": "Fintech Caribe S.A.S.", "canal": "plataforma_digital", "plan": "basico", "permisos": ["cotizar"]}


def socio(estado: str = "activo") -> dict:
    return {
        "id": ID, "nombre": match.like("Fintech Caribe S.A.S."), "canal": match.like("plataforma_digital"),
        "plan": match.like("basico"), "permisos": match.each_like("cotizar"), "estado": estado,
        "clientId": match.like("3n2kq4v0example"), "creado": FECHA,
    }


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / "bff-web-api-socios.json").unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    # Un mock por interacción; write_file las combina.
    pacto = Pact("bff-web", "api-socios").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


async def test_habilitar_socio(pacto: Pact) -> None:
    (pacto.upon_receiving("una solicitud de alta de socio")
        .with_request("POST", "/admin/socios")
        .with_header("X-Authenticated-Sub", "sub-paula")
        .with_body(ALTA, content_type="application/json")
        .will_respond_with(201)
        .with_header("Cache-Control", "no-store")
        .with_body(socio() | {"clientSecret": match.like("secreto"), "apiKey": match.like("api-key")},
                   content_type="application/json"))
    with pacto.serve() as servidor:
        creado = await ApiSocios(str(servidor.url)).crear(ALTA, "sub-paula")
    assert creado["clientSecret"] and creado["apiKey"] and creado["estado"] == "activo"


async def test_listar_socios(pacto: Pact) -> None:
    (pacto.upon_receiving("una consulta del listado de socios")
        .given("existe el socio SOC-000001")
        .with_request("GET", "/admin/socios")
        .will_respond_with(200)
        .with_body(match.each_like(socio()), content_type="application/json"))
    with pacto.serve() as servidor:
        socios = await ApiSocios(str(servidor.url)).listar(None)
    assert socios[0]["id"] == "SOC-000001" and "clientSecret" not in socios[0]


async def test_consultar_socio(pacto: Pact) -> None:
    (pacto.upon_receiving("una consulta del detalle de un socio")
        .given("existe el socio SOC-000001")
        .with_request("GET", "/admin/socios/SOC-000001")
        .will_respond_with(200)
        .with_body(socio(), content_type="application/json"))
    with pacto.serve() as servidor:
        assert (await ApiSocios(str(servidor.url)).obtener("SOC-000001", None))["id"] == "SOC-000001"


async def test_consultar_socio_inexistente(pacto: Pact) -> None:
    (pacto.upon_receiving("una consulta de un socio inexistente")
        .with_request("GET", "/admin/socios/SOC-999999")
        .will_respond_with(404)
        .with_body({"codigo": "SOCIO_NO_ENCONTRADO", "mensaje": match.like("El socio no existe"),
                    "correlationId": match.like("c0rr3l4c10n"), "detalles": {}}, content_type="application/json"))
    with pacto.serve() as servidor, pytest.raises(ErrorApiSocios) as error:
        await ApiSocios(str(servidor.url)).obtener("SOC-999999", None)
    assert (error.value.status, error.value.codigo) == (404, "SOCIO_NO_ENCONTRADO")


async def test_desactivar_socio(pacto: Pact) -> None:
    (pacto.upon_receiving("una desactivación de socio")
        .given("existe el socio SOC-000001")
        .with_request("POST", "/admin/socios/SOC-000001/desactivar")
        .will_respond_with(200)
        .with_body(socio("desactivado") | {"desactivado": FECHA}, content_type="application/json"))
    with pacto.serve() as servidor:
        desactivado = await ApiSocios(str(servidor.url)).desactivar("SOC-000001", None)
    assert desactivado["estado"] == "desactivado"


LIMITES = {"plan": match.like("prueba"), "limitePorSegundo": match.like(1.0), "rafaga": match.like(2),
           "cuota": match.like(5), "periodo": match.regex("DAY", regex=r"^(DAY|WEEK|MONTH)$")}
DIA = match.regex("2026-10-13", regex=r"^\d{4}-\d{2}-\d{2}$")


async def test_listar_planes(pacto: Pact) -> None:
    (pacto.upon_receiving("una consulta de los planes de uso disponibles")
        .with_request("GET", "/admin/socios/planes")
        .will_respond_with(200)
        .with_body(match.each_like(LIMITES), content_type="application/json"))
    with pacto.serve() as servidor:
        planes = await ApiSocios(str(servidor.url)).planes(None)
    assert planes[0]["cuota"] > 0


async def test_cambiar_plan(pacto: Pact) -> None:
    (pacto.upon_receiving("un cambio de plan de un socio")
        .given("existe el socio SOC-000001")
        .with_request("PUT", "/admin/socios/SOC-000001/plan")
        .with_header("X-Authenticated-Sub", "sub-paula")
        .with_body({"plan": "estandar"}, content_type="application/json")
        .will_respond_with(200)
        .with_body(socio() | {"plan": "estandar"}, content_type="application/json"))
    with pacto.serve() as servidor:
        cambiado = await ApiSocios(str(servidor.url)).cambiar_plan("SOC-000001", "estandar", "sub-paula")
    assert cambiado["plan"] == "estandar"


async def test_consultar_consumo(pacto: Pact) -> None:
    (pacto.upon_receiving("una consulta del consumo de un socio")
        .given("existe el socio SOC-000001")
        .with_request("GET", "/admin/socios/SOC-000001/consumo")
        .will_respond_with(200)
        .with_body({
            "socioId": ID, "limites": LIMITES,
            "ventana": {"inicio": DIA, "fin": DIA, "reinicio": FECHA},
            "usado": match.like(0), "restante": match.like(1000), "porcentaje": match.like(0),
            "estado": match.regex("normal", regex=r"^(normal|cerca_del_limite|agotada)$"),
        }, content_type="application/json"))
    with pacto.serve() as servidor:
        consumo = await ApiSocios(str(servidor.url)).consumo("SOC-000001", None)
    assert consumo["socioId"] == "SOC-000001" and "apiKeyId" not in consumo
