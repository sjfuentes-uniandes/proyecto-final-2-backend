"""Compatibilidad hacia atrás del contrato público del socio (HU-W04 AC2).

`contracts/openapi/publicados/api-socios.v1.yaml` es la versión v1 publicada a los socios: las
operaciones del socio ya disponibles (sin `/admin`, que es interno, ni `/health`, ni las que aún
responden 501).
El contrato que sirve la app debe seguir siendo compatible con ella: un consumidor escrito contra
v1 la sigue interpretando. Es incompatible (y la prueba falla) que:

- desaparezca una operación, un código de respuesta o un parámetro;
- se elimine o se renombre un campo de un esquema, o cambie su tipo;
- un campo o parámetro opcional de la solicitud pase a obligatorio, o aparezca uno obligatorio;
- un campo obligatorio de la respuesta pase a opcional (el consumidor cuenta con él);
- desaparezca un valor de un enum.

Agregar operaciones, códigos, campos opcionales en la solicitud o campos en la respuesta es
compatible. Un cambio incompatible exige una versión nueva (v2) publicada aparte.

Congelar una versión nueva (solo al publicarla, nunca para "arreglar" esta prueba):
    uv run python services/api-socios/tests/contract/test_contrato_publicado.py
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from api_socios.main import app

PUBLICADO = (
    Path(__file__).parents[4] / "contracts" / "openapi" / "publicados" / "api-socios.v1.yaml"
)
REF = "#/components/schemas/"


def _refs(nodo: Any) -> set[str]:
    if isinstance(nodo, dict):
        propias = {nodo["$ref"].removeprefix(REF)} if "$ref" in nodo else set()
        return propias | {r for v in nodo.values() for r in _refs(v)}
    if isinstance(nodo, list):
        return {r for v in nodo for r in _refs(v)}
    return set()


def _alcanzables(raices: set[str], esquemas: dict[str, Any]) -> set[str]:
    vistos: set[str] = set()
    pendientes = list(raices)
    while pendientes:
        nombre = pendientes.pop()
        if nombre not in vistos:
            vistos.add(nombre)
            pendientes.extend(_refs(esquemas.get(nombre, {})))
    return vistos


def contrato_del_socio(openapi: dict[str, Any]) -> dict[str, Any]:
    """Las operaciones del socio ya disponibles y solo los esquemas que usan."""
    rutas: dict[str, Any] = {}
    for ruta, operaciones in openapi["paths"].items():
        if ruta.startswith("/admin") or ruta == "/health":
            continue
        disponibles = {m: o for m, o in operaciones.items() if "501" not in o["responses"]}
        if disponibles:
            rutas[ruta] = disponibles
    esquemas = openapi["components"]["schemas"]
    usados = _alcanzables(_refs(rutas), esquemas)
    return {
        "openapi": openapi["openapi"],
        "info": {"title": "API de socios de Solventa", "version": "v1"},
        "paths": rutas,
        "components": {"schemas": {n: esquemas[n] for n in sorted(usados)}},
    }


def _roles(contrato: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(esquemas de solicitud, esquemas de respuesta), con los anidados."""
    esquemas = contrato["components"]["schemas"]
    solicitud: set[str] = set()
    respuesta: set[str] = set()
    for operaciones in contrato["paths"].values():
        for operacion in operaciones.values():
            solicitud |= _refs(operacion.get("requestBody", {}))
            respuesta |= _refs(operacion["responses"])
    return _alcanzables(solicitud, esquemas), _alcanzables(respuesta, esquemas)


def _forma(propiedad: dict[str, Any]) -> tuple[Any, ...]:
    """Lo que no puede cambiar de un campo: tipo, formato y referencia (también en listas y en
    los anyOf de los opcionales)."""
    variantes = propiedad.get("anyOf", [propiedad])
    return tuple(
        (v.get("type"), v.get("format"), v.get("$ref"), (v.get("items") or {}).get("$ref"))
        for v in variantes
    )


def incompatibilidades(publicado: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    errores: list[str] = []
    for ruta, operaciones in publicado["paths"].items():
        for metodo, operacion in operaciones.items():
            nueva = actual["paths"].get(ruta, {}).get(metodo)
            donde = f"{metodo.upper()} {ruta}"
            if nueva is None:
                errores.append(f"{donde}: operación eliminada")
                continue
            for codigo in operacion["responses"]:
                if codigo not in nueva["responses"]:
                    errores.append(f"{donde}: respuesta {codigo} eliminada")
            antes = {(p["name"], p["in"]): p for p in operacion.get("parameters", [])}
            ahora = {(p["name"], p["in"]): p for p in nueva.get("parameters", [])}
            for clave, parametro in antes.items():
                if clave not in ahora:
                    errores.append(f"{donde}: parámetro {clave[0]} eliminado")
                elif ahora[clave].get("required") and not parametro.get("required"):
                    errores.append(f"{donde}: parámetro {clave[0]} pasó a obligatorio")
            for clave, parametro in ahora.items():
                if clave not in antes and parametro.get("required"):
                    errores.append(f"{donde}: parámetro obligatorio nuevo {clave[0]}")
    de_solicitud, de_respuesta = _roles(publicado)
    esquemas = actual["components"]["schemas"]
    for nombre, esquema in publicado["components"]["schemas"].items():
        nuevo = esquemas.get(nombre)
        if nuevo is None:
            errores.append(f"{nombre}: esquema eliminado o renombrado")
            continue
        if set(esquema.get("enum", [])) - set(nuevo.get("enum", [])):
            errores.append(f"{nombre}: valores del enum eliminados")
        propiedades, nuevas = esquema.get("properties", {}), nuevo.get("properties", {})
        requeridos, nuevos_requeridos = (
            set(esquema.get("required", [])),
            set(nuevo.get("required", [])),
        )
        for campo, propiedad in propiedades.items():
            if campo not in nuevas:
                errores.append(f"{nombre}.{campo}: campo eliminado o renombrado")
            elif _forma(propiedad) != _forma(nuevas[campo]):
                errores.append(f"{nombre}.{campo}: cambió su tipo")
        if nombre in de_solicitud:
            for campo in nuevos_requeridos - requeridos:
                errores.append(f"{nombre}.{campo}: pasó a obligatorio en la solicitud")
        if nombre in de_respuesta:
            for campo in requeridos - nuevos_requeridos:
                errores.append(f"{nombre}.{campo}: dejó de ser obligatorio en la respuesta")
    return errores


def _publicado() -> dict[str, Any]:
    contrato: dict[str, Any] = yaml.safe_load(PUBLICADO.read_text(encoding="utf-8"))
    return contrato


def test_el_contrato_actual_es_compatible_con_el_v1_publicado() -> None:
    assert incompatibilidades(_publicado(), contrato_del_socio(app.openapi())) == []


def test_el_v1_publicado_contiene_la_oferta() -> None:
    publicado = _publicado()
    assert set(publicado["paths"]) == {
        "/socio/me",
        "/cotizaciones",
        "/cotizaciones/{cotizacion_id}",
    }
    assert "410" in publicado["paths"]["/cotizaciones/{cotizacion_id}"]["get"]["responses"]
    assert not any(r.startswith("/admin") for r in publicado["paths"])


# --- El comparador detecta cada cambio incompatible ----------------------------------------


@pytest.fixture
def v1() -> dict[str, Any]:
    return copy.deepcopy(_publicado())


def _esquema(contrato: dict[str, Any], nombre: str) -> dict[str, Any]:
    esquema: dict[str, Any] = contrato["components"]["schemas"][nombre]
    return esquema


def test_un_campo_eliminado_es_incompatible(v1: dict[str, Any]) -> None:
    actual = copy.deepcopy(v1)
    del _esquema(actual, "OfertaV1")["properties"]["precio"]
    assert "OfertaV1.precio: campo eliminado o renombrado" in incompatibilidades(v1, actual)


def test_un_campo_renombrado_es_incompatible(v1: dict[str, Any]) -> None:
    actual = copy.deepcopy(v1)
    oferta = _esquema(actual, "OfertaV1")
    oferta["properties"]["valor"] = oferta["properties"].pop("precio")
    oferta["required"] = ["valor" if c == "precio" else c for c in oferta["required"]]
    errores = incompatibilidades(v1, actual)
    assert "OfertaV1.precio: campo eliminado o renombrado" in errores
    assert "OfertaV1.precio: dejó de ser obligatorio en la respuesta" in errores


def test_un_opcional_que_pasa_a_obligatorio_es_incompatible(v1: dict[str, Any]) -> None:
    actual = copy.deepcopy(v1)
    _esquema(actual, "SolicitudCotizacionV1")["required"].append("coberturasOpcionales")
    assert incompatibilidades(v1, actual) == [
        "SolicitudCotizacionV1.coberturasOpcionales: pasó a obligatorio en la solicitud"
    ]


def test_un_campo_obligatorio_nuevo_en_la_solicitud_es_incompatible(v1: dict[str, Any]) -> None:
    actual = copy.deepcopy(v1)
    solicitud = _esquema(actual, "SolicitudCotizacionV1")
    solicitud["properties"]["canal"] = {"type": "string"}
    solicitud["required"].append("canal")
    assert incompatibilidades(v1, actual) == [
        "SolicitudCotizacionV1.canal: pasó a obligatorio en la solicitud"
    ]


def test_un_tipo_cambiado_es_incompatible(v1: dict[str, Any]) -> None:
    actual = copy.deepcopy(v1)
    _esquema(actual, "OfertaV1")["properties"]["version"] = {"type": "string"}
    assert incompatibilidades(v1, actual) == ["OfertaV1.version: cambió su tipo"]


def test_operaciones_respuestas_parametros_y_enums(v1: dict[str, Any]) -> None:
    actual = copy.deepcopy(v1)
    del actual["paths"]["/cotizaciones/{cotizacion_id}"]["get"]["responses"]["410"]
    del actual["paths"]["/socio/me"]
    _esquema(actual, "EstadoOferta")["enum"].remove("VENCIDA")
    post = actual["paths"]["/cotizaciones"]["post"]
    post["parameters"].append({"name": "X-Canal", "in": "header", "required": True})
    assert sorted(incompatibilidades(v1, actual)) == [
        "EstadoOferta: valores del enum eliminados",
        "GET /cotizaciones/{cotizacion_id}: respuesta 410 eliminada",
        "GET /socio/me: operación eliminada",
        "POST /cotizaciones: parámetro obligatorio nuevo X-Canal",
    ]


def test_los_cambios_aditivos_son_compatibles(v1: dict[str, Any]) -> None:
    actual = copy.deepcopy(v1)
    _esquema(actual, "OfertaV1")["properties"]["descuento"] = {"type": "string"}
    _esquema(actual, "OfertaV1")["required"].append("descuento")
    _esquema(actual, "SolicitudCotizacionV1")["properties"]["referencia"] = {"type": "string"}
    _esquema(actual, "EstadoOferta")["enum"].append("ACEPTADA")
    actual["paths"]["/ofertas"] = {"get": {"responses": {"200": {}}}}
    assert incompatibilidades(v1, actual) == []


if __name__ == "__main__":
    PUBLICADO.parent.mkdir(parents=True, exist_ok=True)
    PUBLICADO.write_text(
        yaml.safe_dump(contrato_del_socio(app.openapi()), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
