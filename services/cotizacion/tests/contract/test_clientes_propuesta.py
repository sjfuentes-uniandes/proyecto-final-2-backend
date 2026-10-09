"""CT-W10-01: los DTO de ClientesHttp, el stub (CS-08) y la propuesta de clientes coinciden.

- Cada mapeo 200 de mappings/clientes-stub/ se valida con IdentidadDto o VerificacionDto.
- Los campos obligatorios y los enum de los DTO son los de
  contracts/openapi/clientes.propuesta-cotizacion.yaml.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from cotizacion.adapters.outbound.clientes_http import (
    AlcanceRechazadoDto,
    IdentidadDto,
    VerificacionDto,
)
from pydantic import BaseModel

RAIZ = Path(__file__).parents[4]
PROPUESTA = yaml.safe_load(
    (RAIZ / "contracts" / "openapi" / "clientes.propuesta-cotizacion.yaml").read_text(
        encoding="utf-8"
    )
)
ESQUEMAS: dict[str, Any] = PROPUESTA["components"]["schemas"]
STUB = sorted(
    (RAIZ / "services" / "simulador-aliados" / "mappings" / "clientes-stub").glob("*.json")
)


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


def _enum_propuesta(esquema: dict[str, Any]) -> set[Any] | None:
    esquema = _resolver(esquema, ESQUEMAS)
    if "items" in esquema:
        return _enum_propuesta(esquema["items"])
    return set(esquema["enum"]) if "enum" in esquema else None


@pytest.mark.parametrize(
    ("dto", "nombre"),
    [
        (IdentidadDto, "Identidad"),
        (VerificacionDto, "Verificacion"),
        (AlcanceRechazadoDto, "AlcanceRechazado"),
    ],
)
def test_required_y_enum_coinciden_con_la_propuesta(dto: type[BaseModel], nombre: str) -> None:
    esquema_dto = dto.model_json_schema(by_alias=True)
    definiciones = esquema_dto.get("$defs", {})
    propuesta = ESQUEMAS[nombre]
    assert set(esquema_dto["required"]) == set(propuesta["required"])
    assert set(esquema_dto["properties"]) == set(propuesta["properties"])
    for campo, definicion in propuesta["properties"].items():
        assert _enum(esquema_dto["properties"][campo], definiciones) == _enum_propuesta(
            definicion
        ), campo


@pytest.mark.parametrize("ruta", STUB, ids=lambda r: r.name)
def test_cada_respuesta_200_del_stub_cumple_los_dto(ruta: Path) -> None:
    mapeo = json.loads(ruta.read_text(encoding="utf-8"))
    respuesta = mapeo["response"]
    if respuesta["status"] != 200:
        assert respuesta["jsonBody"]["codigo"] == "CLIENTE_NO_ENCONTRADO"
        return
    dto = IdentidadDto if mapeo["request"]["urlPath"].endswith("/identidad") else VerificacionDto
    dto.model_validate(respuesta["jsonBody"])


def test_el_stub_cubre_los_19_clientes() -> None:
    assert len([r for r in STUB if "no-encontrado" not in r.name]) == 2 * 19
