"""CT-W13-02: pacto consumidor `polizas` → proveedor `cotizacion` para los insumos de la oferta.

Lo genera `make test-contract SERVICE=polizas` en `contracts/pact/polizas-cotizacion.json`. Cada
interacción ejercita el adaptador real (`CotizacionHttp.insumos`) contra el mock de Pact: el pacto
describe exactamente lo que `polizas` envía (el actor ASESOR) y lo que sabe leer. `cotizacion` lo
verifica como proveedor (`make test-contract SERVICE=cotizacion`).

Solo contiene respuestas que `cotizacion` produce (200 con perfil, 200 sin perfil y 404
COTIZACION_NO_ENCONTRADA); la falla cerrada ante timeout o 5xx se prueba en
`tests/unit/suscripcion/test_adaptadores_http.py`.
"""

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pact import Pact, match
from polizas.adapters.outbound.cotizacion_http import CotizacionHttp
from polizas.domain.suscripcion.errores import CotizacionNoEncontrada
from polizas.domain.suscripcion.modelos import Actor, RolActor
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "polizas-cotizacion.json"
ASESOR = Actor("sub-asesor-pacto", RolActor.ASESOR)
NO_EXISTE = "00000000-0000-4000-a000-0000000000ff"
FECHA = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
DECIMAL = r"^-?\d+(\.\d+)?$"
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    # Un mock por interacción; write_file las combina.
    pacto = Pact("polizas", "cotizacion").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


def _cuerpo(insumos: dict[str, Any]) -> dict[str, Any]:
    """Lo que polizas exige: el formato de cada campo y los conjuntos cerrados; el valor exacto
    lo pone el proveedor."""
    perfil = insumos["perfil"]
    return {
        "cotizacionId": match.regex(insumos["cotizacionId"], regex=UUID_RE),
        "ofertaId": None,
        "codigo": None,
        "clienteId": match.regex(insumos["clienteId"], regex=UUID_RE),
        "estadoIdentidad": match.regex(
            insumos["estadoIdentidad"], regex="^(VERIFICADO|PENDIENTE|RECHAZADO)$"
        ),
        "socioId": None,
        "productoId": match.like(insumos["productoId"]),
        "versionProducto": match.like(insumos["versionProducto"]),
        "moneda": match.regex(insumos["moneda"], regex=r"^[A-Z]{3}$"),
        "periodicidad": match.like(insumos["periodicidad"]),
        "primaTotal": match.regex(insumos["primaTotal"], regex=DECIMAL),
        "versionReglasPrima": match.like(insumos["versionReglasPrima"]),
        "huellaCalculo": match.like(insumos["huellaCalculo"]),
        "coberturas": match.each_like(insumos["coberturas"][0], min=1),
        "sumaAsegurada": match.regex(insumos["sumaAsegurada"], regex=DECIMAL),
        "plazoCreditoMeses": match.integer(insumos["plazoCreditoMeses"]),
        "edad": match.integer(insumos["edad"]),
        "calculadoEn": match.regex(insumos["calculadoEn"], regex=FECHA),
        "vigenteHasta": match.regex(insumos["vigenteHasta"], regex=FECHA),
        "perfil": None
        if perfil is None
        else {
            "id": match.regex(perfil["id"], regex=UUID_RE),
            "estado": match.regex(
                perfil["estado"], regex="^(CALCULADO|DEGRADADO|INFORMACION_INSUFICIENTE)$"
            ),
            "nivelRiesgo": match.regex(perfil["nivelRiesgo"], regex="^(BAJO|MEDIO|ALTO)$"),
            "confianza": match.regex(perfil["confianza"], regex=DECIMAL),
            "fuentesConsultadas": match.integer(perfil["fuentesConsultadas"]),
            "fuentesDeclaradas": match.integer(perfil["fuentesDeclaradas"]),
        },
        "coincidenciasListasRestrictivas": None
        if insumos["coincidenciasListasRestrictivas"] is None
        else match.integer(insumos["coincidenciasListasRestrictivas"]),
    }


def _pedido(pacto: Pact, descripcion: str, estado: str, cotizacion: str) -> Any:
    return (
        pacto.upon_receiving(descripcion)
        .given(estado)
        .with_request("GET", f"/v1/cotizaciones/{cotizacion}/insumos-suscripcion")
        .with_header("X-Actor-Id", ASESOR.id)
        .with_header("X-Actor-Rol", "ASESOR")
    )


async def _insumos(url: str, cotizacion: str) -> Any:
    async with correlation.cliente_async() as cliente:
        return await CotizacionHttp(cliente, url, 2000).insumos(UUID(cotizacion), ASESOR)


@pytest.mark.parametrize(
    ("id_sintetico", "descripcion", "estado"),
    [
        (
            "S01",
            "los insumos de una cotización con perfil calculado",
            "existe la cotización S01 de un cliente verificado con su perfil CALCULADO",
        ),
        (
            "S08",
            "los insumos de una cotización sin perfil",
            "existe la cotización S08 de un cliente verificado, calculada sin perfil",
        ),
    ],
)
async def test_insumos_de_una_cotizacion(
    pacto: Pact, sinteticos: Any, id_sintetico: str, descripcion: str, estado: str
) -> None:
    insumos = sinteticos.caso(id_sintetico)["insumos"]
    (
        _pedido(pacto, descripcion, estado, insumos["cotizacionId"])
        .will_respond_with(200)
        .with_body(_cuerpo(insumos), content_type="application/json")
    )
    with pacto.serve() as servidor:
        leidos = await _insumos(str(servidor.url), insumos["cotizacionId"])
    esperados = sinteticos.insumos(id_sintetico)
    # El mock responde un elemento por cada each_like: se comparan las coberturas aparte.
    assert leidos.coberturas == esperados.coberturas[:1]
    assert dataclasses.replace(leidos, coberturas=esperados.coberturas) == esperados


async def test_cotizacion_inexistente(pacto: Pact) -> None:
    (
        _pedido(
            pacto,
            "los insumos de una cotización inexistente",
            f"la cotización {NO_EXISTE} no existe",
            NO_EXISTE,
        )
        .will_respond_with(404)
        .with_body(
            {
                "codigo": "COTIZACION_NO_ENCONTRADA",
                "mensaje": match.like("La cotización no existe"),
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor, pytest.raises(CotizacionNoEncontrada):
        await _insumos(str(servidor.url), NO_EXISTE)
