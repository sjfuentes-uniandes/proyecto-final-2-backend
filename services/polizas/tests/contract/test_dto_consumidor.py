"""Las copias del consumidor en polizas coinciden con los contratos de sus proveedores (HU-W13):
- `InsumosSuscripcionDto` con `InsumosSuscripcion` de contracts/openapi/cotizacion.yaml;
- los conjuntos cerrados de `catalogo_http` con contracts/modelos/reglas-suscripcion.v1.schema.json.
La verificación del proveedor con Pact (CT-W13-02) llega con los insumos de cotizacion (B4).
"""

import json
from pathlib import Path
from typing import Any, get_args

import yaml
from polizas.adapters.outbound import catalogo_http
from polizas.adapters.outbound.catalogo_http import a_reglas
from polizas.adapters.outbound.cotizacion_http import InsumosSuscripcionDto, a_insumos

CONTRATOS = Path(__file__).parents[4] / "contracts"


def _esquemas_cotizacion() -> dict[str, Any]:
    texto = (CONTRATOS / "openapi" / "cotizacion.yaml").read_text(encoding="utf-8")
    esquemas: dict[str, Any] = yaml.safe_load(texto)["components"]["schemas"]
    return esquemas


def test_los_campos_de_insumos_son_los_del_proveedor() -> None:
    publicado = _esquemas_cotizacion()["InsumosSuscripcion"]
    propios = {campo.alias for campo in InsumosSuscripcionDto.model_fields.values()}
    assert propios == set(publicado["properties"])
    assert set(publicado["required"]) == propios


def test_los_insumos_sinteticos_se_leen(id_caso: str, sinteticos: Any) -> None:
    contenido = json.dumps(sinteticos.caso(id_caso)["insumos"]).encode()
    assert a_insumos(contenido) == sinteticos.insumos(id_caso)


def test_los_conjuntos_cerrados_son_los_del_contrato_de_reglas() -> None:
    esquema = json.loads(
        (CONTRATOS / "modelos" / "reglas-suscripcion.v1.schema.json").read_text(encoding="utf-8")
    )
    definiciones = esquema["$defs"]
    assert list(get_args(catalogo_http.Campo)) == definiciones["CampoRegla"]["enum"]
    assert list(get_args(catalogo_http.Operador)) == definiciones["OperadorRegla"]["enum"]
    assert list(get_args(catalogo_http.Motivo)) == definiciones["MotivoCliente"]["enum"]


def test_la_semilla_de_catalogo_se_lee(sinteticos: Any) -> None:
    contenido = json.dumps(sinteticos.respuesta_reglas()).encode()
    assert a_reglas(contenido) == sinteticos.reglas()
