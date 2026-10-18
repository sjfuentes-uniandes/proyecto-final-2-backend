"""Contrato: los DTO de ClientesHttp y el doble de `clientes` de las pruebas de módulo cumplen el
contrato publicado de `clientes` (`contracts/openapi/clientes.yaml`).

- Los campos obligatorios, las propiedades y los enum de los DTO son los de `clientes.yaml`.
- Cada mapeo del doble (`tests/dobles/clientes/`) es una ruta de `clientes.yaml`, su cuerpo valida
  el esquema publicado y los DTO, y responde lo que dicen los datos sintéticos de su caso.

El doble sustituye al stub retirado del simulador (PR 3.6); la PR 3.7 lleva las pruebas de
módulo a `clientes` real. La verificación de `clientes` también la fija el pacto
(`test_pact_clientes.py`), que `clientes` verifica como proveedor.
"""

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from cotizacion.adapters.outbound.clientes_http import (
    AlcanceRechazadoDto,
    IdentidadDto,
    VerificacionDto,
)
from pydantic import BaseModel

RAIZ = Path(__file__).parents[4]
CONTRATO: dict[str, Any] = yaml.safe_load(
    (RAIZ / "contracts" / "openapi" / "clientes.yaml").read_text(encoding="utf-8")
)
ESQUEMAS: dict[str, Any] = CONTRATO["components"]["schemas"]
CASOS: list[dict[str, Any]] = json.loads(
    (RAIZ / "contracts" / "datos-sinteticos" / "clientes-perfilamiento.json").read_text(
        encoding="utf-8"
    )
)
DOBLE = sorted((Path(__file__).parents[1] / "dobles" / "clientes").glob("*.json"))
PREFIJO = "/clientes"
RUTA_IDENTIDAD = "/v1/clientes/{cliente_id}/identidad"
RUTA_VERIFICACION = "/v1/clientes/{clienteId}/consentimientos/verificaciones"
ALCANCES = ESQUEMAS["Alcance"]["enum"]
VIGENTE_HASTA = "2027-10-01T00:00:00Z"
VERIFICADO_EN = "2026-10-01T00:00:00Z"


def _resolver(esquema: dict[str, Any], raiz: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in esquema:
        esquema = raiz[esquema["$ref"].rsplit("/", 1)[-1]]
    return esquema


def _enum(esquema: dict[str, Any], raiz: dict[str, Any]) -> set[Any] | None:
    """Valores del enum de una propiedad (atraviesa $ref, anyOf con null y arrays)."""
    esquema = _resolver(esquema, raiz)
    if "items" in esquema:
        return _enum(esquema["items"], raiz)
    if "enum" in esquema:
        return set(esquema["enum"])
    if "const" in esquema:  # Literal de un solo valor
        return {esquema["const"]}
    for opcion in esquema.get("anyOf", []):
        valores = _enum(opcion, raiz)
        if valores is not None:
            return valores | (
                {None} if any(o.get("type") == "null" for o in esquema["anyOf"]) else set()
            )
    return None


def _respuesta(ruta: str, metodo: str, estado: int) -> dict[str, Any]:
    respuesta = CONTRATO["paths"][ruta][metodo]["responses"][str(estado)]
    return dict(respuesta["content"]["application/json"]["schema"])


def _publicado(ruta: str, metodo: str) -> dict[str, Any]:
    return _resolver(_respuesta(ruta, metodo, 200), ESQUEMAS)


VERIFICACION = _publicado(RUTA_VERIFICACION, "post")


@pytest.mark.parametrize(
    ("dto", "publicado"),
    [
        (IdentidadDto, _publicado(RUTA_IDENTIDAD, "get")),
        (VerificacionDto, VERIFICACION),
        (
            AlcanceRechazadoDto,
            _resolver(VERIFICACION["properties"]["alcancesRechazados"]["items"], ESQUEMAS),
        ),
    ],
    ids=["identidad", "verificacion", "alcance-rechazado"],
)
def test_required_y_enum_coinciden_con_clientes_yaml(
    dto: type[BaseModel], publicado: dict[str, Any]
) -> None:
    esquema_dto = dto.model_json_schema(by_alias=True)
    definiciones = esquema_dto.get("$defs", {})
    assert set(esquema_dto["required"]) == set(publicado["required"])
    assert set(esquema_dto["properties"]) == set(publicado["properties"])
    for campo, definicion in publicado["properties"].items():
        assert _enum(esquema_dto["properties"][campo], definiciones) == _enum(
            definicion, ESQUEMAS
        ), campo


def _ruta_del_contrato(mapeo: dict[str, Any]) -> str:
    solicitud = mapeo["request"]
    ruta = solicitud.get("urlPath") or solicitud["urlPathPattern"]
    assert ruta.startswith(f"{PREFIJO}/")
    ruta = ruta.removeprefix(PREFIJO)
    return next(
        plantilla
        for plantilla in (RUTA_IDENTIDAD, RUTA_VERIFICACION)
        if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", plantilla), ruta)
        or ruta == re.sub(r"\{[^}]+\}", "[^/]+", plantilla)
    )


@pytest.mark.parametrize("ruta", DOBLE, ids=lambda r: r.name)
def test_cada_respuesta_del_doble_cumple_clientes_yaml_y_los_dto(ruta: Path) -> None:
    mapeo = json.loads(ruta.read_text(encoding="utf-8"))
    plantilla = _ruta_del_contrato(mapeo)
    metodo = mapeo["request"]["method"].lower()
    respuesta = mapeo["response"]
    esquema = _respuesta(plantilla, metodo, respuesta["status"])
    jsonschema.Draft202012Validator({**esquema, "components": CONTRATO["components"]}).validate(
        respuesta["jsonBody"]
    )
    if respuesta["status"] != 200:
        assert respuesta["status"] == 404
        assert respuesta["jsonBody"]["codigo"] == "CLIENTE_NO_ENCONTRADO"
        return
    dto = IdentidadDto if plantilla == RUTA_IDENTIDAD else VerificacionDto
    dto.model_validate(respuesta["jsonBody"])


def _mapeo(nombre: str) -> dict[str, Any]:
    ruta = next(r for r in DOBLE if r.name == nombre)
    return dict(json.loads(ruta.read_text(encoding="utf-8")))


def test_el_doble_cubre_los_19_clientes_y_el_404() -> None:
    assert len(CASOS) == 19
    assert len(DOBLE) == 2 * 19 + 2
    respaldos = [_mapeo(r.name) for r in DOBLE if "no-encontrado" in r.name]
    assert len(respaldos) == 2
    for mapeo in respaldos:
        # Prioridad menor que la de los casos (5 por omisión): solo responde a ids desconocidos.
        assert mapeo["priority"] == 10
        assert mapeo["response"]["status"] == 404


@pytest.mark.parametrize("caso", CASOS, ids=lambda c: c["id"])
def test_el_doble_responde_lo_que_dicen_los_datos_sinteticos(caso: dict[str, Any]) -> None:
    nombre = caso["id"].lower()
    identidad = _mapeo(f"{nombre}-identidad.json")
    assert identidad["request"]["urlPath"] == f"{PREFIJO}/v1/clientes/{caso['clienteId']}/identidad"
    assert identidad["response"]["jsonBody"] == {
        "clienteId": caso["clienteId"],
        "estado": caso["identidad"],
        "verificadoEn": VERIFICADO_EN if caso["identidad"] == "VERIFICADO" else None,
    }

    verificacion = _mapeo(f"{nombre}-consentimiento.json")
    assert verificacion["request"]["urlPath"] == (
        f"{PREFIJO}/v1/clientes/{caso['clienteId']}/consentimientos/verificaciones"
    )
    assert verificacion["request"]["bodyPatterns"] == [
        {"matchesJsonPath": "$[?(@.proposito == 'PERFILAMIENTO_PRECIO')]"}
    ]
    esperado = caso["consentimiento"]
    permitidos = esperado["alcancesPermitidos"]
    assert verificacion["response"]["jsonBody"] == {
        "decision": esperado["decision"],
        "consentimientoId": esperado["consentimientoId"],
        "vigenteHasta": None if esperado["decision"] == "RECHAZADO" else VIGENTE_HASTA,
        "alcancesPermitidos": permitidos,
        "alcancesRechazados": [
            {"alcance": alcance, "causa": "ALCANCE_NO_AUTORIZADO"}
            for alcance in ALCANCES
            if alcance not in permitidos
        ],
        "causa": esperado["causa"],
    }


# --- Escenarios de HU-W31 (PR 3.4): vigente en la fase A, distinto en la comprobación final --

ESCENARIOS = sorted(
    (Path(__file__).parents[1] / "dobles" / "clientes" / "escenarios").glob("*.json")
)
# escenario → (respuesta de la comprobación final, si cumple el contrato)
ESPERADOS = {"revocacion-en-vuelo-c01": "REVOCADO", "caida-al-comprobar-c01": None}


def _escenario(nombre: str) -> dict[str, Any]:
    ruta = next(r for r in ESCENARIOS if r.name == nombre)
    return dict(json.loads(ruta.read_text(encoding="utf-8")))


def test_los_escenarios_son_dos_por_caso_y_solo_se_activan_a_pedido() -> None:
    mapeos = [json.loads(r.read_text(encoding="utf-8")) for r in ESCENARIOS]
    assert len(mapeos) == 2 * len(ESPERADOS)
    assert {m["scenarioName"] for m in mapeos} == set(ESPERADOS)
    for mapeo in mapeos:
        # Nunca en el estado inicial (Started): las demás pruebas de C01 no los ven.
        assert mapeo["requiredScenarioState"] in {"fase-a", "comprobacion-final"}
        # Prioridad mayor que la de C01 (5 por omisión) solo mientras el escenario está activo.
        assert mapeo["priority"] == 1
        assert mapeo["request"] == _mapeo("c01-consentimiento.json")["request"]
        assert _ruta_del_contrato(mapeo) == RUTA_VERIFICACION
        transicion = mapeo.get("newScenarioState")
        assert transicion == (
            "comprobacion-final" if mapeo["requiredScenarioState"] == "fase-a" else None
        )


@pytest.mark.parametrize("escenario", sorted(ESPERADOS))
def test_la_fase_a_del_escenario_responde_lo_mismo_que_c01(escenario: str) -> None:
    prefijo = escenario.removesuffix("-c01")
    fase_a = _escenario(f"{prefijo}-fase-a.json")
    assert fase_a["response"] == _mapeo("c01-consentimiento.json")["response"]


@pytest.mark.parametrize("ruta", ESCENARIOS, ids=lambda r: r.name)
def test_cada_respuesta_200_de_los_escenarios_cumple_clientes_yaml_y_los_dto(ruta: Path) -> None:
    respuesta = json.loads(ruta.read_text(encoding="utf-8"))["response"]
    if respuesta["status"] != 200:
        # Falla técnica simulada (5xx sin cuerpo): fuera del contrato a propósito.
        assert respuesta["status"] >= 500 and "jsonBody" not in respuesta
        return
    esquema = _respuesta(RUTA_VERIFICACION, "post", 200)
    jsonschema.Draft202012Validator({**esquema, "components": CONTRATO["components"]}).validate(
        respuesta["jsonBody"]
    )
    VerificacionDto.model_validate(respuesta["jsonBody"])


@pytest.mark.parametrize("escenario", sorted(ESPERADOS))
def test_la_comprobacion_final_del_escenario(escenario: str) -> None:
    final = _escenario(f"{escenario.removesuffix('-c01')}-comprobacion-final.json")
    causa = ESPERADOS[escenario]
    if causa is None:
        assert final["response"] == {"status": 500}
        return
    cuerpo = final["response"]["jsonBody"]
    assert (cuerpo["decision"], cuerpo["causa"], cuerpo["consentimientoId"]) == (
        "RECHAZADO",
        causa,
        None,
    )
    assert cuerpo["alcancesPermitidos"] == []
