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
        "/v1/clientes/actual/consentimientos",
        "/v1/clientes/actual/consentimientos/{consentimientoId}",
        "/v1/clientes/actual/consentimientos/{consentimientoId}/revocacion",
        "/v1/clientes/actual/consentimientos/{consentimientoId}/historial",
        "/v1/consentimientos/textos/vigente",
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


def test_identidad_es_compatible_con_la_propuesta_de_cotizacion() -> None:
    """cotizacion lee {clienteId, estado, verificadoEn} de GET .../identidad."""
    propuesta = yaml.safe_load(
        (CONTRATO.parent / "clientes.propuesta-cotizacion.yaml").read_text(encoding="utf-8")
    )["components"]["schemas"]["Identidad"]
    publicado = app.openapi()["components"]["schemas"]["IdentidadDto"]
    assert set(publicado["required"]) == set(propuesta["required"])
    assert set(publicado["properties"]) == set(propuesta["properties"])
