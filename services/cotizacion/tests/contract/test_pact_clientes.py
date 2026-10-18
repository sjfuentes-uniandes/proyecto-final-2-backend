"""Pacto consumidor `cotizacion` → proveedor `clientes`: verificación del consentimiento antes de
consultar datos financieros (HU-W31).

Lo genera `make test-contract SERVICE=cotizacion` en `contracts/pact/cotizacion-clientes.json`.
Cada interacción ejercita el adaptador real (`ClientesHttp.verificar`) contra el mock de Pact,
de modo que el pacto describe exactamente lo que `cotizacion` envía y lo que sabe leer.

Solo contiene respuestas que el contrato de verificación define que `clientes` devuelve (`200`
con cada decisión y cada causa, y `404 CLIENTE_NO_ENCONTRADO`): la PR 3.2 lo verifica contra
`clientes` real. El fallo cerrado ante timeout, 5xx o cuerpos fuera de contrato no es algo que
el proveedor produzca; se prueba en `tests/unit/perfilamiento/test_clientes_http.py`.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import jsonschema
import pytest
import yaml
from cotizacion.adapters.outbound.clientes_http import ClientesHttp
from cotizacion.domain.perfilamiento.errores import ClienteNoEncontrado
from cotizacion.domain.perfilamiento.modelos import (
    PROPOSITO_PERFILAMIENTO,
    DecisionConsentimiento,
    Fuente,
)
from pact import Pact, match
from solventa_common import correlation

PACTOS = Path(__file__).parents[4] / "contracts" / "pact"
ARCHIVO = "cotizacion-clientes.json"
CLIENTE = "00000000-0000-4000-8000-000000000001"
NO_EXISTE = "00000000-0000-4000-8000-0000000000ff"
CONSENTIMIENTO_ID = "00000000-0000-4000-9000-000000000001"
FECHA = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
ALCANCE_RE = "^(" + "|".join(Fuente) + ")$"
TODAS = list(Fuente)
# cotizacion siempre pide los cuatro alcances para el propósito único (CrearPerfil, fase A).
SOLICITUD = {"proposito": PROPOSITO_PERFILAMIENTO, "alcances": [str(f) for f in TODAS]}
pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def _ruta(cliente: str) -> str:
    return f"/v1/clientes/{cliente}/consentimientos/verificaciones"


def _alcance(valor: Fuente) -> object:
    return match.regex(str(valor), regex=ALCANCE_RE)


def _rechazado(valor: Fuente) -> dict[str, object]:
    return {"alcance": _alcance(valor), "causa": "ALCANCE_NO_AUTORIZADO"}


def permitida(
    decision: str, permitidos: list[Fuente], rechazados: list[Fuente]
) -> dict[str, object]:
    return {
        "decision": decision,
        "consentimientoId": match.regex(CONSENTIMIENTO_ID, regex=UUID_RE),
        "vigenteHasta": match.regex("2027-10-17T23:59:59Z", regex=FECHA),
        "alcancesPermitidos": match.each_like(_alcance(permitidos[0]), min=1),
        "alcancesRechazados": (
            match.each_like(_rechazado(rechazados[0]), min=1) if rechazados else []
        ),
        "causa": None,
    }


def rechazada(causa: str) -> dict[str, object]:
    # Sin consentimiento utilizable: nada permitido y los alcances pedidos, rechazados.
    return {
        "decision": "RECHAZADO",
        "consentimientoId": None,
        "vigenteHasta": None,
        "alcancesPermitidos": [],
        "alcancesRechazados": match.each_like(_rechazado(TODAS[0]), min=1),
        "causa": causa,
    }


def _adaptador(url: str) -> ClientesHttp:
    return ClientesHttp(correlation.cliente_async(), url, 2000)


@pytest.fixture(scope="module", autouse=True)
def pacto_nuevo() -> None:
    (PACTOS / ARCHIVO).unlink(missing_ok=True)


@pytest.fixture
def pacto() -> Iterator[Pact]:
    # Un mock por interacción; write_file las combina.
    pacto = Pact("cotizacion", "clientes").with_specification("V4")
    yield pacto
    pacto.write_file(PACTOS)


def _verificacion(pacto: Pact, descripcion: str, estado: str, cliente: str = CLIENTE) -> Any:
    return (
        pacto.upon_receiving(descripcion)
        .given(estado)
        .with_request("POST", _ruta(cliente))
        .with_body(SOLICITUD, content_type="application/json")
    )


async def test_consentimiento_vigente(pacto: Pact) -> None:
    (
        _verificacion(
            pacto,
            "una verificación con un consentimiento vigente que cubre los alcances",
            f"el cliente {CLIENTE} tiene un consentimiento ACTIVO y vigente para "
            "PERFILAMIENTO_PRECIO con los cuatro alcances",
        )
        .will_respond_with(200)
        .with_body(permitida("PERMITIDO", TODAS, []), content_type="application/json")
    )
    with pacto.serve() as servidor:
        verificacion = await _adaptador(str(servidor.url)).verificar(
            UUID(CLIENTE), PROPOSITO_PERFILAMIENTO, TODAS
        )
    assert verificacion.decision is DecisionConsentimiento.PERMITIDO
    assert str(verificacion.consentimiento_id) == CONSENTIMIENTO_ID
    assert verificacion.vigente_hasta is not None
    assert verificacion.alcances_permitidos and verificacion.causa is None


async def test_alcance_no_autorizado(pacto: Pact) -> None:
    permitidos = [f for f in TODAS if f is not Fuente.FA_INGRESOS_AGREGADOS]
    (
        _verificacion(
            pacto,
            "una verificación con un consentimiento vigente que no cubre un alcance",
            f"el cliente {CLIENTE} tiene un consentimiento ACTIVO y vigente para "
            "PERFILAMIENTO_PRECIO sin el alcance FA_INGRESOS_AGREGADOS",
        )
        .will_respond_with(200)
        .with_body(
            permitida("PERMITIDO_PARCIAL", permitidos, [Fuente.FA_INGRESOS_AGREGADOS]),
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor:
        verificacion = await _adaptador(str(servidor.url)).verificar(
            UUID(CLIENTE), PROPOSITO_PERFILAMIENTO, TODAS
        )
    assert verificacion.decision is DecisionConsentimiento.PERMITIDO_PARCIAL
    assert verificacion.alcances_permitidos <= set(permitidos)
    assert verificacion.causa is None


@pytest.mark.parametrize(
    ("causa", "descripcion", "estado"),
    [
        (
            "SIN_CONSENTIMIENTO",
            "una verificación de un cliente que nunca otorgó consentimiento",
            f"el cliente {CLIENTE} existe y no tiene consentimientos",
        ),
        (
            "REVOCADO",
            "una verificación con el consentimiento revocado",
            (
                f"el cliente {CLIENTE} revocó su consentimiento para PERFILAMIENTO_PRECIO "
                "y no tiene otro ACTIVO"
            ),
        ),
        (
            "VENCIDO",
            "una verificación con el consentimiento vencido",
            (
                f"el consentimiento del cliente {CLIENTE} para PERFILAMIENTO_PRECIO llegó a "
                "su vigenteHasta y no tiene otro ACTIVO"
            ),
        ),
        (
            "PROPOSITO_NO_AUTORIZADO",
            "una verificación para un propósito que el cliente no autorizó",
            f"el cliente {CLIENTE} solo tiene un consentimiento ACTIVO para otro propósito",
        ),
    ],
)
async def test_consentimiento_no_vigente(
    pacto: Pact, causa: str, descripcion: str, estado: str
) -> None:
    (
        _verificacion(pacto, descripcion, estado)
        .will_respond_with(200)
        .with_body(rechazada(causa), content_type="application/json")
    )
    with pacto.serve() as servidor:
        verificacion = await _adaptador(str(servidor.url)).verificar(
            UUID(CLIENTE), PROPOSITO_PERFILAMIENTO, TODAS
        )
    assert verificacion.decision is DecisionConsentimiento.RECHAZADO
    assert verificacion.causa == causa
    assert verificacion.alcances_permitidos == frozenset()
    assert (verificacion.consentimiento_id, verificacion.vigente_hasta) == (None, None)


async def test_cliente_inexistente(pacto: Pact) -> None:
    (
        _verificacion(
            pacto,
            "una verificación de un cliente inexistente",
            f"el cliente {NO_EXISTE} no existe",
            NO_EXISTE,
        )
        .will_respond_with(404)
        .with_body(
            {
                "codigo": "CLIENTE_NO_ENCONTRADO",
                "mensaje": match.like("Cliente no encontrado"),
                "correlationId": match.like("c0rr3l4c10n"),
                "detalles": {},
            },
            content_type="application/json",
        )
    )
    with pacto.serve() as servidor, pytest.raises(ClienteNoEncontrado):
        await _adaptador(str(servidor.url)).verificar(
            UUID(NO_EXISTE), PROPOSITO_PERFILAMIENTO, TODAS
        )


def _resolver(nodo: dict[str, Any], contrato: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in nodo:
        destino: Any = contrato
        for parte in nodo["$ref"].removeprefix("#/").split("/"):
            destino = destino[parte]
        nodo = destino
    return nodo


def test_el_pacto_cumple_el_contrato_de_verificacion() -> None:
    """Mientras `clientes` no verifique el pacto (PR 3.2), se coteja con el contrato vigente de
    verificación (`clientes.propuesta-cotizacion.yaml`): cada interacción existe allí, la
    solicitud y los ejemplos validan su esquema, y están todas las decisiones y causas.

    Corre al final del módulo: lee el pacto que escribieron las pruebas anteriores.
    """
    contrato = yaml.safe_load(
        (PACTOS.parent / "openapi" / "clientes.propuesta-cotizacion.yaml").read_text(
            encoding="utf-8"
        )
    )
    pacto = json.loads((PACTOS / ARCHIVO).read_text(encoding="utf-8"))
    assert len(pacto["interactions"]) == 7
    componentes = {"components": contrato["components"]}
    vistas: set[tuple[int, object, object]] = set()
    for interaccion in pacto["interactions"]:
        solicitud, respuesta = interaccion["request"], interaccion["response"]
        assert interaccion["providerStates"], interaccion["description"]
        plantilla = next(
            ruta
            for ruta in contrato["paths"]
            if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", ruta), solicitud["path"])
        )
        operacion = contrato["paths"][plantilla][solicitud["method"].lower()]
        pedido = operacion["requestBody"]["content"]["application/json"]["schema"]
        jsonschema.Draft202012Validator({**pedido, **componentes}).validate(
            solicitud["body"]["content"]
        )
        declarada = _resolver(operacion["responses"][str(respuesta["status"])], contrato)
        esquema = declarada["content"]["application/json"]["schema"]
        cuerpo = respuesta["body"]["content"]
        jsonschema.Draft202012Validator({**esquema, **componentes}).validate(cuerpo)
        vistas.add(
            (
                respuesta["status"],
                cuerpo.get("decision"),
                cuerpo.get("causa") or cuerpo.get("codigo"),
            )
        )
    verificacion = contrato["components"]["schemas"]["Verificacion"]["properties"]
    causas = {c for c in verificacion["causa"]["enum"] if c is not None}
    assert {d for _, d, _ in vistas if d} == set(verificacion["decision"]["enum"])
    assert {c for s, d, c in vistas if d == "RECHAZADO"} == causas
    assert (404, None, "CLIENTE_NO_ENCONTRADO") in vistas
