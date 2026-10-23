"""HU-W04: pacto consumidor `api-socios` → proveedor `cotizacion` (la oferta del socio).

Lo genera `make test-contract SERVICE=api-socios` en `contracts/pact/api-socios-cotizacion.json` y
lo verifica `cotizacion` como proveedor (`make test-contract SERVICE=cotizacion`). Cada interacción
ejercita el adaptador real (`CotizacionHttp`) contra el mock de Pact: describe lo que api-socios
envía (el socio como actor SOCIO, sin socioId ni perfilId) y lo único que lee de la respuesta
(`productoId` y `oferta`; lector tolerante, el resto de la cotización no forma parte del pacto).

Solo respuestas que `cotizacion` produce: 201 al cotizar, 200 al leer, 404 de la cotización de otro
socio y 422 de un producto vencido. Los timeouts y 5xx se prueban en tests/unit/test_ofertas.py.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from api_socios.adapters.outbound.cotizacion_http import CotizacionHttp
from api_socios.domain.ofertas import (
    CotizacionRechazada,
    EstadoOferta,
    OfertaNoEncontrada,
    SolicitudOferta,
)
from pact import Pact, match

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "api-socios-cotizacion.json"
SOCIO = "SOC-000001"
CLAVE = UUID("00000000-0000-4000-c000-000000000004")
DEL_SOCIO = "00000000-0000-4000-d000-000000000004"
DE_OTRO = "00000000-0000-4000-d000-000000000005"
FECHA = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
DECIMAL = r"^-?\d+(\.\d+)?$"
SOLICITUD = SolicitudOferta(
    cliente_id=UUID("00000000-0000-4000-8000-000000000001"),
    producto_id="vida-hipotecario",
    fecha_nacimiento=date(1990, 4, 18),
    plazo_credito_meses=180,
    suma_asegurada=180000000,
    coberturas_opcionales=("ENFERMEDADES_GRAVES",),
)
CUERPO = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
    "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
}
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
    pacto = Pact("api-socios", "cotizacion").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


def _oferta(cotizacion_id: str) -> dict[str, Any]:
    """Lo que api-socios exige de la oferta: el formato de cada campo y los conjuntos cerrados."""
    return {
        "id": match.regex("00000000-0000-4000-e000-000000000004", regex=UUID_RE),
        "codigo": match.regex("COT-2026-00004", regex=r"^COT-\d{4}-\d{5,}$"),
        "cotizacionId": match.regex(cotizacion_id, regex=UUID_RE),
        "precio": match.regex("97460", regex=DECIMAL),
        "moneda": match.regex("COP", regex=r"^[A-Z]{3}$"),
        "periodicidad": match.like("MENSUAL"),
        "coberturas": match.each_like(
            {
                "codigo": match.like("MUERTE"),
                "nombre": match.like("Muerte por cualquier causa"),
                "sumaAsegurada": match.regex("180000000", regex=DECIMAL),
            },
            min=1,
        ),
        "exclusionesResumidas": match.each_like(
            {
                "codigo": match.like("SUICIDIO_PRIMER_ANIO"),
                "descripcion": match.like("Suicidio durante el primer año"),
            },
            min=1,
        ),
        "version": match.integer(1),
        "versionReglas": match.like("2026.10.1"),
        "creadaEn": match.regex("2026-10-22T15:00:00Z", regex=FECHA),
        "vigenteHasta": match.regex("2026-10-29T15:00:00Z", regex=FECHA),
        "estado": match.regex("VIGENTE", regex="^(VIGENTE|VENCIDA)$"),
    }


def _cotizacion(cotizacion_id: str) -> dict[str, Any]:
    return {"productoId": match.like("vida-hipotecario"), "oferta": _oferta(cotizacion_id)}


def _adaptador(url: str) -> CotizacionHttp:
    return CotizacionHttp(url, 2000)


async def test_cotizar_como_socio(pacto: Pact) -> None:
    (
        pacto.upon_receiving(
            "una cotización de un socio con reglas que traen condiciones de oferta"
        )
        .given("las reglas vigentes de vida-hipotecario traen condiciones de oferta")
        .with_request("POST", "/v1/cotizaciones")
        .with_header("X-Actor-Id", SOCIO)
        .with_header("X-Actor-Rol", "SOCIO")
        .with_header("Idempotency-Key", match.regex(str(CLAVE), regex=UUID_RE))
        .with_body(CUERPO, content_type="application/json")
        .will_respond_with(201)
        .with_body(_cotizacion(DEL_SOCIO), content_type="application/json")
    )
    with pacto.serve() as servidor:
        oferta, creada = await _adaptador(str(servidor.url)).cotizar(SOLICITUD, SOCIO, CLAVE)
    assert creada is True
    assert (oferta.codigo, oferta.estado, oferta.producto_id) == (
        "COT-2026-00004",
        EstadoOferta.VIGENTE,
        "vida-hipotecario",
    )


async def test_un_producto_vencido_se_rechaza_con_su_codigo(pacto: Pact) -> None:
    cuerpo = CUERPO | {"productoId": "vida-hipotecario-2025", "coberturasOpcionales": []}
    (
        pacto.upon_receiving("una cotización de un socio para un producto vencido")
        .given("las reglas vigentes de vida-hipotecario traen condiciones de oferta")
        .with_request("POST", "/v1/cotizaciones")
        .with_header("X-Actor-Id", SOCIO)
        .with_header("X-Actor-Rol", "SOCIO")
        .with_header("Idempotency-Key", match.regex(str(CLAVE), regex=UUID_RE))
        .with_body(cuerpo, content_type="application/json")
        .will_respond_with(422)
        .with_body(
            {"codigo": "PRODUCTO_FUERA_DE_VIGENCIA", "mensaje": match.like("m")},
            content_type="application/json",
        )
    )
    solicitud = replace(SOLICITUD, producto_id="vida-hipotecario-2025", coberturas_opcionales=())
    with pacto.serve() as servidor, pytest.raises(CotizacionRechazada) as rechazo:
        await _adaptador(str(servidor.url)).cotizar(solicitud, SOCIO, CLAVE)
    assert (rechazo.value.codigo, rechazo.value.estado_http) == ("PRODUCTO_FUERA_DE_VIGENCIA", 422)


async def test_leer_la_oferta_del_socio(pacto: Pact) -> None:
    (
        pacto.upon_receiving("la lectura de una cotización del socio")
        .given(f"existe la cotización {DEL_SOCIO} del socio {SOCIO} con su oferta")
        .with_request("GET", f"/v1/cotizaciones/{DEL_SOCIO}")
        .with_header("X-Actor-Id", SOCIO)
        .with_header("X-Actor-Rol", "SOCIO")
        .will_respond_with(200)
        .with_body(_cotizacion(DEL_SOCIO), content_type="application/json")
    )
    with pacto.serve() as servidor:
        oferta = await _adaptador(str(servidor.url)).oferta(UUID(DEL_SOCIO), SOCIO)
    assert oferta.cotizacion_id == UUID(DEL_SOCIO)


async def test_la_cotizacion_de_otro_socio_no_existe(pacto: Pact) -> None:
    (
        pacto.upon_receiving("la lectura de una cotización de otro socio")
        .given(f"existe la cotización {DE_OTRO} de otro socio")
        .with_request("GET", f"/v1/cotizaciones/{DE_OTRO}")
        .with_header("X-Actor-Id", SOCIO)
        .with_header("X-Actor-Rol", "SOCIO")
        .will_respond_with(404)
        .with_body(
            {"codigo": "COTIZACION_NO_ENCONTRADA", "mensaje": match.like("m")},
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor, pytest.raises(OfertaNoEncontrada):
        await _adaptador(str(servidor.url)).oferta(UUID(DE_OTRO), SOCIO)
