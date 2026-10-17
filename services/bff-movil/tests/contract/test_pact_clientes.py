"""Pacto consumidor `bff-movil` → proveedor `clientes`: rutas de consentimiento (HU-M08).

Lo genera `make test-contract SERVICE=bff-movil` en `contracts/pact/bff-movil-clientes.json`.
Cada interacción ejercita el adaptador real (`ConsentimientosHttp`) contra el mock de Pact, de
modo que el pacto describe exactamente lo que el BFF envía y lo que sabe leer.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import jsonschema
import pytest
import yaml
from bff_movil.adapters.outbound.consentimientos_http import ConsentimientosHttp
from bff_movil.domain.consentimientos.modelos import (
    ConsentimientoNoEncontrado,
    ConsentimientoYaVigente,
    EstadoConsentimiento,
    IdentidadNoVerificada,
    SolicitudOtorgamiento,
    SolicitudRechazada,
)
from pact import Pact, match
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "bff-movil-clientes.json"
SUB = "22222222-2222-4222-8222-222222222222"
CLAVE = UUID("6f1c2b9e-3d4a-4e8f-9b7c-1a2b3c4d5e6f")
CONSENTIMIENTO_ID = "00000000-0000-4000-9000-000000000001"
RUTA = "/v1/clientes/actual/consentimientos"
FECHA = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SOLICITUD = SolicitudOtorgamiento(
    "PERFILAMIENTO_PRECIO", ("FA_PRODUCTOS_VIGENTES",), 12, "2026-10-17.1", "es-CO"
)
CUERPO = {
    "proposito": "PERFILAMIENTO_PRECIO",
    "alcances": ["FA_PRODUCTOS_VIGENTES"],
    "vigenciaMeses": 12,
    "versionTexto": "2026-10-17.1",
    "idioma": "es-CO",
}
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def consentimiento(estado: str = "ACTIVO") -> dict[str, object]:
    return {
        "consentimientoId": match.regex(CONSENTIMIENTO_ID, regex=UUID_RE),
        "estado": match.regex(estado, regex=r"^(ACTIVO|REVOCADO|VENCIDO)$"),
        "proposito": match.like("PERFILAMIENTO_PRECIO"),
        "alcances": match.each_like("FA_PRODUCTOS_VIGENTES"),
        "versionTexto": match.like("2026-10-17.1"),
        "idioma": match.like("es-CO"),
        "otorgadoEn": match.regex("2026-10-17T15:00:00Z", regex=FECHA),
        "vigenteHasta": match.regex("2027-10-17T23:59:59Z", regex=FECHA),
        "revocadoEn": None,
    }


def error(codigo: str, detalles: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "codigo": codigo,
        "mensaje": match.like("Mensaje genérico"),
        "correlationId": match.like("c0rr3l4c10n"),
        "detalles": detalles or {},
    }


def _con_actor(interaccion):
    return interaccion.with_header("X-Actor-Id", SUB).with_header("X-Actor-Rol", "CLIENTE")


def _adaptador(url: str) -> ConsentimientosHttp:
    return ConsentimientosHttp(correlation.cliente_async(), url, 2000, 2000)


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    # Un mock por interacción; write_file las combina.
    pacto = Pact("bff-movil", "clientes").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


def _otorgamiento(pacto: Pact, descripcion: str):
    return _con_actor(
        pacto.upon_receiving(descripcion)
        .with_request("POST", RUTA)
        .with_header("Idempotency-Key", match.regex(str(CLAVE), regex=UUID_RE))
    ).with_body(CUERPO, content_type="application/json")


async def test_otorgar(pacto: Pact) -> None:
    (
        _otorgamiento(pacto, "un otorgamiento de consentimiento")
        .given("el titular tiene identidad VERIFICADO y ningún consentimiento vigente")
        .will_respond_with(201)
        .with_body(consentimiento(), content_type="application/json")
    )
    with pacto.serve() as servidor:
        otorgamiento = await _adaptador(str(servidor.url)).otorgar(SUB, CLAVE, SOLICITUD)
    assert otorgamiento.repeticion is False
    assert otorgamiento.consentimiento.estado is EstadoConsentimiento.ACTIVO


async def test_repetir_la_clave(pacto: Pact) -> None:
    (
        _otorgamiento(pacto, "la repetición de un otorgamiento con la misma clave")
        .given("el titular ya otorgó con esa Idempotency-Key y el mismo cuerpo")
        .will_respond_with(200)
        .with_header("Idempotent-Replayed", "true")
        .with_body(consentimiento(), content_type="application/json")
    )
    with pacto.serve() as servidor:
        otorgamiento = await _adaptador(str(servidor.url)).otorgar(SUB, CLAVE, SOLICITUD)
    assert otorgamiento.repeticion is True


async def test_otorgar_con_consentimiento_vigente(pacto: Pact) -> None:
    (
        _otorgamiento(pacto, "un otorgamiento cuando ya hay uno vigente")
        .given("el titular ya tiene un consentimiento ACTIVO para PERFILAMIENTO_PRECIO")
        .will_respond_with(409)
        .with_body(
            error(
                "CONSENTIMIENTO_YA_VIGENTE",
                {"consentimientoId": match.regex(CONSENTIMIENTO_ID, regex=UUID_RE)},
            ),
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor, pytest.raises(ConsentimientoYaVigente) as falla:
        await _adaptador(str(servidor.url)).otorgar(SUB, CLAVE, SOLICITUD)
    assert str(falla.value.consentimiento_id) == CONSENTIMIENTO_ID


async def test_otorgar_sin_identidad_verificada(pacto: Pact) -> None:
    (
        _otorgamiento(pacto, "un otorgamiento de un titular sin identidad verificada")
        .given("el titular tiene identidad PENDIENTE")
        .will_respond_with(403)
        .with_body(error("IDENTIDAD_NO_VERIFICADA"), content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(IdentidadNoVerificada):
        await _adaptador(str(servidor.url)).otorgar(SUB, CLAVE, SOLICITUD)


async def test_otorgar_incompleto(pacto: Pact) -> None:
    vacio = dict.fromkeys(CUERPO)
    (
        _con_actor(
            pacto.upon_receiving("un otorgamiento sin propósito, alcances ni vigencia")
            .given("el titular tiene identidad VERIFICADO y ningún consentimiento vigente")
            .with_request("POST", RUTA)
            .with_header("Idempotency-Key", match.regex(str(CLAVE), regex=UUID_RE))
        )
        .with_body(vacio, content_type="application/json")
        .will_respond_with(422)
        .with_body(
            error("SOLICITUD_INVALIDA", {"campos": match.each_like("proposito")}),
            content_type="application/json",
        )
    )
    solicitud = SolicitudOtorgamiento(None, None, None, None, None)
    with pacto.serve() as servidor, pytest.raises(SolicitudRechazada) as falla:
        await _adaptador(str(servidor.url)).otorgar(SUB, CLAVE, solicitud)
    assert falla.value.codigo == "SOLICITUD_INVALIDA"
    assert falla.value.campos


async def test_listar(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("una consulta de los consentimientos del titular")
            .given("el titular tiene un consentimiento ACTIVO")
            .with_request("GET", RUTA)
        )
        .will_respond_with(200)
        .with_body(match.each_like(consentimiento()), content_type="application/json")
    )
    with pacto.serve() as servidor:
        consentimientos = await _adaptador(str(servidor.url)).listar(SUB)
    assert str(consentimientos[0].consentimiento_id) == CONSENTIMIENTO_ID


async def test_detalle(pacto: Pact) -> None:
    (
        _con_actor(
            pacto.upon_receiving("una consulta del detalle de un consentimiento propio")
            .given("el titular tiene el consentimiento 00000000-0000-4000-9000-000000000001")
            .with_request("GET", f"{RUTA}/{CONSENTIMIENTO_ID}")
        )
        .will_respond_with(200)
        .with_body(consentimiento(), content_type="application/json")
    )
    with pacto.serve() as servidor:
        detalle = await _adaptador(str(servidor.url)).detalle(SUB, UUID(CONSENTIMIENTO_ID))
    assert detalle.version_texto == "2026-10-17.1"


async def test_detalle_ajeno_o_inexistente(pacto: Pact) -> None:
    ajeno = "00000000-0000-4000-9000-0000000000ff"
    (
        _con_actor(
            pacto.upon_receiving("una consulta del detalle de un consentimiento ajeno")
            .given("el consentimiento 00000000-0000-4000-9000-0000000000ff es de otro cliente")
            .with_request("GET", f"{RUTA}/{ajeno}")
        )
        .will_respond_with(404)
        .with_body(error("CONSENTIMIENTO_NO_ENCONTRADO"), content_type="application/json")
    )
    with pacto.serve() as servidor, pytest.raises(ConsentimientoNoEncontrado):
        await _adaptador(str(servidor.url)).detalle(SUB, UUID(ajeno))


async def test_texto_vigente(pacto: Pact) -> None:
    (
        pacto.upon_receiving("una consulta del texto vigente")
        .with_request("GET", "/v1/consentimientos/textos/vigente")
        .with_query_parameter("proposito", "PERFILAMIENTO_PRECIO")
        .with_query_parameter("idioma", "es-CO")
        .will_respond_with(200)
        .with_body(
            {
                "proposito": "PERFILAMIENTO_PRECIO",
                "idioma": "es-CO",
                "versionTexto": match.like("2026-10-17.1"),
                "titulo": match.like("Autorización de ejemplo"),
                "cuerpo": match.like("Texto ilustrativo."),
                "aviso": match.like("Sin validez legal"),
                "vigenciasMeses": match.each_like(12),
                "vigenciaDefectoMeses": match.like(12),
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        texto = await _adaptador(str(servidor.url)).texto_vigente("PERFILAMIENTO_PRECIO", "es-CO")
    assert texto.vigencia_defecto_meses in texto.vigencias_meses


def test_el_pacto_cumple_el_openapi_publicado_por_clientes() -> None:
    """Mientras `clientes` no verifique el pacto (PR propia), se coteja con su contrato: cada
    interacción existe en `contracts/openapi/clientes.yaml` y sus ejemplos validan su esquema.

    Corre al final del módulo: lee el pacto que escribieron las pruebas anteriores.
    """
    openapi = yaml.safe_load(
        (PACTOS.parent / "openapi" / "clientes.yaml").read_text(encoding="utf-8")
    )
    pacto = json.loads((PACTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert len(pacto["interactions"]) == 9
    for interaccion in pacto["interactions"]:
        solicitud, respuesta = interaccion["request"], interaccion["response"]
        plantilla = next(
            ruta
            for ruta in openapi["paths"]
            if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", ruta), solicitud["path"])
        )
        operacion = openapi["paths"][plantilla][solicitud["method"].lower()]
        declarada = operacion["responses"][str(respuesta["status"])]
        esquema = declarada["content"]["application/json"]["schema"]
        jsonschema.Draft202012Validator({**esquema, "components": openapi["components"]}).validate(
            respuesta["body"]["content"]
        )
        # Content-Type lo describe `content`, no `headers`.
        for cabecera in set(respuesta.get("headers", {})) - {"Content-Type"}:
            assert cabecera in declarada.get("headers", {})
