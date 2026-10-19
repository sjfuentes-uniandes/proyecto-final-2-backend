"""Contrato: los DTO de ClientesHttp cumplen el contrato publicado de `clientes`
(`contracts/openapi/clientes.yaml`): mismos campos obligatorios, propiedades y enum.

Se conserva de `test_dobles_clientes.py` (PR 3.6), que se retiró con el doble de `clientes` en la
PR 3.7: las pruebas de módulo usan ahora `clientes` real (`tests/module/conftest.py`) y la
verificación también la fija el pacto (`test_pact_clientes.py`), que `clientes` verifica como
proveedor.
"""

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
CONTRATO: dict[str, Any] = yaml.safe_load(
    (RAIZ / "contracts" / "openapi" / "clientes.yaml").read_text(encoding="utf-8")
)
ESQUEMAS: dict[str, Any] = CONTRATO["components"]["schemas"]
RUTA_IDENTIDAD = "/v1/clientes/{cliente_id}/identidad"
RUTA_VERIFICACION = "/v1/clientes/{clienteId}/consentimientos/verificaciones"


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


def _publicado(ruta: str, metodo: str) -> dict[str, Any]:
    respuesta = CONTRATO["paths"][ruta][metodo]["responses"]["200"]
    return _resolver(dict(respuesta["content"]["application/json"]["schema"]), ESQUEMAS)


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
