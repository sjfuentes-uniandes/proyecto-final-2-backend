"""El contrato publicado (contracts/openapi/clientes.yaml) es el que sirve la app."""

from pathlib import Path

import yaml
from clientes.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "clientes.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_verificaciones() -> None:
    rutas = app.openapi()["paths"]
    verificar = rutas["/v1/clientes/actual/identidad/verificaciones"]["post"]
    assert set(verificar["responses"]) == {"200", "400", "401", "403", "422", "503"}
    assert set(rutas) == {
        "/v1/clientes/actual",
        "/v1/clientes/{cliente_id}/identidad",
        "/v1/clientes/actual/identidad/verificaciones",
        "/v1/clientes",
        "/v1/clientes/{cliente_id}",
        "/v1/clientes/{cliente_id}/identidad/verificacion-documental",
        "/v1/clientes/actual/consentimientos",
        "/v1/clientes/actual/consentimientos/{consentimientoId}",
        "/v1/clientes/actual/consentimientos/{consentimientoId}/revocacion",
        "/v1/clientes/actual/consentimientos/{consentimientoId}/historial",
        "/v1/consentimientos/textos/vigente",
        "/v1/clientes/{clienteId}/consentimientos/verificaciones",
        "/v1/clientes/{clienteId}/consentimientos",
        "/v1/clientes/{clienteId}/consentimientos/{consentimientoId}/historial",
        "/v1/registros/{registro_id}/verificaciones",
        "/v1/registros/{registro_id}/vinculacion",
        "/health",
    }
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudVerificacion"]["required"]) == {"selfie", "pruebaDeVida"}
    assert set(esquemas["VerificacionDto"]["required"]) == {
        "clienteId",
        "estado",
        "resultado",
        "motivo",
        "verificadoEn",
    }
    assert esquemas["EstadoIdentidad"]["enum"] == ["VERIFICADO", "PENDIENTE", "RECHAZADO"]


def test_contrato_del_alta_y_la_verificacion_documental() -> None:
    """HU-W30: alta idempotente en PENDIENTE y verificación por documento, solo para asesores."""
    rutas = app.openapi()["paths"]
    alta = rutas["/v1/clientes"]["post"]
    assert set(alta["responses"]) == {"200", "201", "400", "401", "403", "422"}
    verificar = rutas["/v1/clientes/{cliente_id}/identidad/verificacion-documental"]["post"]
    assert set(verificar["responses"]) == {"200", "401", "403", "404", "409", "422", "503"}
    assert set(rutas["/v1/clientes/{cliente_id}"]["get"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "422",
    }
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudAltaCliente"]["required"]) == {
        "tipoDocumento",
        "numeroDocumento",
        "nombreCompleto",
        "fechaNacimiento",
    }
    # La respuesta lleva el estado y la traza, nunca PII ni el motivo del proveedor.
    assert set(esquemas["ClienteDto"]["properties"]) == {
        "clienteId",
        "estado",
        "verificadoEn",
        "proveedor",
        "referencia",
    }


def test_contrato_de_consentimientos() -> None:
    """HU-M08: las rutas y respuestas del plan (§c); las rutas de revocar y verificar llegan
    en las Partes 2 y 3."""
    rutas = app.openapi()["paths"]
    base = rutas["/v1/clientes/actual/consentimientos"]
    otorgar = base["post"]
    assert set(otorgar["responses"]) == {"200", "201", "400", "401", "403", "404", "409", "422"}
    assert otorgar["responses"]["200"]["headers"]["Idempotent-Replayed"]
    clave = next(p for p in otorgar["parameters"] if p["name"] == "Idempotency-Key")
    assert clave["required"] is True
    assert clave["schema"]["format"] == "uuid"
    assert set(base["get"]["responses"]) == {"200", "401", "403", "422"}
    detalle = rutas["/v1/clientes/actual/consentimientos/{consentimientoId}"]["get"]
    assert set(detalle["responses"]) == {"200", "401", "403", "404", "422"}
    texto = rutas["/v1/consentimientos/textos/vigente"]["get"]
    assert set(texto["responses"]) == {"200", "422"}

    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["ConsentimientoDto"]["required"]) == {
        "consentimientoId",
        "estado",
        "proposito",
        "alcances",
        "versionTexto",
        "idioma",
        "otorgadoEn",
        "vigenteHasta",
        "revocadoEn",
    }
    assert esquemas["EstadoConsentimiento"]["enum"] == ["ACTIVO", "REVOCADO", "VENCIDO"]
    assert "required" not in esquemas["SolicitudOtorgar"]
    assert set(esquemas["SolicitudOtorgar"]["properties"]) == {
        "proposito",
        "alcances",
        "vigenciaMeses",
        "versionTexto",
        "idioma",
    }
    assert set(esquemas["TextoVigenteDto"]["required"]) == {
        "proposito",
        "idioma",
        "versionTexto",
        "titulo",
        "cuerpo",
        "aviso",
        "vigenciasMeses",
        "vigenciaDefectoMeses",
    }


def test_contrato_de_revocacion_e_historial() -> None:
    """HU-M09 (plan §c): revocar responde 200 la primera vez y en las repeticiones, con el mismo
    `ConsentimientoDto` de la Parte 1; no lleva cuerpo ni Idempotency-Key."""
    rutas = app.openapi()["paths"]
    base = "/v1/clientes/actual/consentimientos/{consentimientoId}"
    revocar = rutas[f"{base}/revocacion"]
    assert set(revocar) == {"post"}
    assert set(revocar["post"]["responses"]) == {"200", "401", "403", "404", "422"}
    assert "requestBody" not in revocar["post"]
    assert [p["name"] for p in revocar["post"]["parameters"]] == [
        "consentimientoId",
        "X-Actor-Id",
        "X-Actor-Rol",
    ]
    ok = revocar["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/ConsentimientoDto"}
    historial = rutas[f"{base}/historial"]
    assert set(historial) == {"get"}
    assert set(historial["get"]["responses"]) == {"200", "401", "403", "404", "422"}

    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["HistorialDto"]["required"]) == {"consentimientoId", "estado", "hitos"}
    assert set(esquemas["HitoDto"]["required"]) == {"tipo", "ocurridoEn"}
    assert esquemas["TipoHito"]["enum"] == ["OTORGADO", "REVOCADO", "VENCIDO"]


def test_los_cambios_de_la_parte_2_son_aditivos() -> None:
    """La app N-1 sigue funcionando: la lista sigue siendo un arreglo de ConsentimientoDto y el
    DTO conserva los campos de la Parte 1."""
    rutas = app.openapi()["paths"]
    lista = rutas["/v1/clientes/actual/consentimientos"]["get"]["responses"]["200"]
    esquema = lista["content"]["application/json"]["schema"]
    assert esquema["type"] == "array"
    assert esquema["items"] == {"$ref": "#/components/schemas/ConsentimientoDto"}
    dto = app.openapi()["components"]["schemas"]["ConsentimientoDto"]
    assert (
        set(dto["properties"])
        == set(dto["required"])
        == {
            "consentimientoId",
            "estado",
            "proposito",
            "alcances",
            "versionTexto",
            "idioma",
            "otorgadoEn",
            "vigenteHasta",
            "revocadoEn",
        }
    )


def test_contrato_de_la_lectura_web() -> None:
    """HU-W31 (plan §c, PR 3.3): solo GET, con actor asesor y los mismos DTO que el titular
    (ADR 0002: en web no hay escritura). Aditivo: las rutas `actual` no cambian."""
    rutas = app.openapi()["paths"]
    base = "/v1/clientes/{clienteId}/consentimientos"
    lista = rutas[base]
    historial = rutas[f"{base}/{{consentimientoId}}/historial"]
    assert set(lista) == set(historial) == {"get"}
    assert set(lista["get"]["responses"]) == {"200", "401", "403", "404", "422"}
    assert set(historial["get"]["responses"]) == {"200", "401", "403", "404", "422"}
    assert [p["name"] for p in lista["get"]["parameters"]] == [
        "clienteId",
        "X-Actor-Id",
        "X-Actor-Rol",
    ]
    assert [p["name"] for p in historial["get"]["parameters"]] == [
        "clienteId",
        "consentimientoId",
        "X-Actor-Id",
        "X-Actor-Rol",
    ]
    assert "ASESOR" in lista["get"]["responses"]["403"]["description"]
    esquema = lista["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert esquema == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/ConsentimientoDto"},
        "title": esquema["title"],
    }
    ok = historial["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/HistorialDto"}
    # La ruta de verificación, en el mismo prefijo, sigue siendo solo POST.
    assert set(rutas[f"{base}/verificaciones"]) == {"post"}


def test_contrato_del_registro() -> None:
    rutas = app.openapi()["paths"]
    verificar = rutas["/v1/registros/{registro_id}/verificaciones"]["post"]
    vincular = rutas["/v1/registros/{registro_id}/vinculacion"]["post"]
    assert set(verificar["responses"]) == {"200", "400", "409", "422", "503"}
    assert set(vincular["responses"]) == {"200", "400", "401", "403", "409", "422"}
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SolicitudVerificacionRegistro"]["required"]) == {
        "correo",
        "selfie",
        "pruebaDeVida",
    }
    assert set(esquemas["SolicitudVinculacion"]["required"]) == {"tokenRegistro"}
    # El token solo viaja con una verificación exitosa: es nulo en los demás casos.
    token = esquemas["VerificacionRegistroDto"]["properties"]["tokenRegistro"]
    assert {"type": "null"} in token["anyOf"]


# Lo que `cotizacion` espera de `clientes`: los valores de la propuesta del consumidor, retirada en
# la PR 3.6. Del lado de `cotizacion`, sus DTO se cotejan con este contrato
# (`test_dto_clientes.py`), y su pacto lo verifica `test_pact_proveedor.py`.
CAMPOS_IDENTIDAD = {"clienteId", "estado", "verificadoEn"}
CAMPOS_VERIFICACION = {
    "decision",
    "consentimientoId",
    "vigenteHasta",
    "alcancesPermitidos",
    "alcancesRechazados",
    "causa",
}


def test_identidad_cumple_lo_que_espera_cotizacion() -> None:
    """cotizacion lee {clienteId, estado, verificadoEn} de GET .../identidad."""
    publicado = app.openapi()["components"]["schemas"]["IdentidadDto"]
    assert set(publicado["required"]) == CAMPOS_IDENTIDAD
    assert set(publicado["properties"]) == CAMPOS_IDENTIDAD


def test_verificacion_cumple_lo_que_espera_cotizacion() -> None:
    """HU-W31 (plan §c): la ruta de verificación cumple sin cambios lo que `cotizacion` espera:
    mismos campos obligatorios, mismos nulos y mismas enumeraciones, y sin encabezados de actor."""
    publicado = app.openapi()["components"]["schemas"]
    ruta = "/v1/clientes/{clienteId}/consentimientos/verificaciones"
    operacion = app.openapi()["paths"][ruta]["post"]

    assert set(operacion["responses"]) == {"200", "400", "404", "422"}
    assert [p["name"] for p in operacion["parameters"]] == ["clienteId"]
    assert operacion["requestBody"]["required"] is True

    solicitud = publicado["SolicitudVerificacionConsentimiento"]
    assert set(solicitud["required"]) == {"proposito", "alcances"}
    assert solicitud["properties"]["alcances"]["minItems"] == 1
    assert publicado["Proposito"]["enum"] == ["PERFILAMIENTO_PRECIO"]
    assert publicado["Alcance"]["enum"] == [
        "FA_PRODUCTOS_VIGENTES",
        "FA_HISTORIAL_PAGOS_12M",
        "FA_INGRESOS_AGREGADOS",
        "DA_REGISTROS_PUBLICOS",
    ]

    respuesta = publicado["VerificacionConsentimientoDto"]
    assert set(respuesta["required"]) == CAMPOS_VERIFICACION
    assert set(respuesta["properties"]) == CAMPOS_VERIFICACION
    for nullable in ("consentimientoId", "vigenteHasta", "causa"):
        assert {"type": "null"} in respuesta["properties"][nullable]["anyOf"]
    assert publicado["Decision"]["enum"] == ["PERMITIDO", "PERMITIDO_PARCIAL", "RECHAZADO"]
    assert publicado["CausaRechazo"]["enum"] + [None] == [
        "SIN_CONSENTIMIENTO",
        "REVOCADO",
        "VENCIDO",
        "PROPOSITO_NO_AUTORIZADO",
        None,
    ]
    assert publicado["CausaAlcance"]["enum"] == ["ALCANCE_NO_AUTORIZADO"]
    assert set(publicado["AlcanceRechazadoDto"]["required"]) == {"alcance", "causa"}


def test_los_enums_de_la_verificacion_son_los_del_dominio() -> None:
    from clientes.domain.consentimientos.modelos import ALCANCES, PROPOSITOS

    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["Alcance"]["enum"]) == ALCANCES
    assert set(esquemas["Proposito"]["enum"]) == PROPOSITOS
