"""El contrato publicado (contracts/openapi/bff-movil.yaml) es el que sirve la app."""

from pathlib import Path

import yaml
from bff_movil.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "bff-movil.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_sesion() -> None:
    operacion = app.openapi()["paths"]["/v1/sesion"]["get"]
    assert set(operacion["responses"]) == {"200", "401", "403", "404", "422", "503"}
    assert [p["name"] for p in operacion["parameters"]] == [
        "x-authenticated-sub",
        "x-authenticated-issuer",
    ]
    assert operacion["security"] == [{"bearerAuth": []}]
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["SesionDto"]["required"]) == {
        "clienteId",
        "serviciosAutorizados",
        "expiraEn",
    }
    assert esquemas["ServicioAutorizado"]["enum"] == [
        "POLIZAS",
        "SINIESTROS",
        "PRIVACIDAD",
        "VERIFICACION_IDENTIDAD",
    ]


def test_contrato_de_consentimientos() -> None:
    """HU-M08: rutas sin `clienteId` (el titular sale del token) y lista como arreglo simple."""
    rutas = app.openapi()["paths"]
    otorgar = rutas["/v1/consentimientos"]["post"]
    assert set(otorgar["responses"]) == {
        "200",
        "201",
        "400",
        "401",
        "403",
        "404",
        "409",
        "422",
        "503",
    }
    assert "Idempotent-Replayed" in otorgar["responses"]["200"]["headers"]
    clave = next(p for p in otorgar["parameters"] if p["name"] == "Idempotency-Key")
    assert clave["required"] is True and clave["schema"]["format"] == "uuid"
    listar = rutas["/v1/consentimientos"]["get"]
    assert listar["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
    assert set(rutas["/v1/consentimientos/{consentimientoId}"]["get"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "422",
        "503",
    }
    assert "/v1/consentimientos/texto-vigente" in rutas
    for ruta, operaciones in rutas.items():
        if ruta.startswith("/v1/consentimientos"):
            for operacion in operaciones.values():
                assert operacion["security"] == [{"bearerAuth": []}]
                assert all("cliente" not in p["name"].lower() for p in operacion["parameters"])
    esquemas = app.openapi()["components"]["schemas"]
    assert esquemas["EstadoConsentimiento"]["enum"] == ["ACTIVO", "REVOCADO", "VENCIDO"]
    assert "required" not in esquemas["SolicitudOtorgar"]


def test_contrato_de_revocacion_e_historial() -> None:
    """HU-M09 (plan §c, tabla del BFF): revocar sin cuerpo ni Idempotency-Key y responde el mismo
    `ConsentimientoDto` de la Parte 1; el historial es de solo lectura."""
    rutas = app.openapi()["paths"]
    base = "/v1/consentimientos/{consentimientoId}"
    revocar = rutas[f"{base}/revocacion"]
    assert set(revocar) == {"post"}
    assert set(revocar["post"]["responses"]) == {"200", "401", "403", "404", "422", "503"}
    assert "requestBody" not in revocar["post"]
    assert all(p["name"] != "Idempotency-Key" for p in revocar["post"]["parameters"])
    ok = revocar["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/ConsentimientoDto"}
    historial = rutas[f"{base}/historial"]
    assert set(historial) == {"get"}
    assert set(historial["get"]["responses"]) == {"200", "401", "403", "404", "422", "503"}
    for operacion in (revocar["post"], historial["get"]):
        assert operacion["security"] == [{"bearerAuth": []}]
        assert "X-Correlation-Id" in operacion["responses"]["200"]["headers"]
    esquemas = app.openapi()["components"]["schemas"]
    assert set(esquemas["HistorialDto"]["required"]) == {"consentimientoId", "estado", "hitos"}
    assert set(esquemas["HitoDto"]["required"]) == {"tipo", "ocurridoEn"}
    assert esquemas["TipoHito"]["enum"] == ["OTORGADO", "REVOCADO", "VENCIDO"]


def test_los_cambios_de_la_parte_2_son_aditivos() -> None:
    """La app N-1 sigue funcionando: las rutas de la Parte 1 siguen, la lista es un arreglo de
    `ConsentimientoDto` y el DTO conserva sus campos."""
    rutas = app.openapi()["paths"]
    for ruta in (
        "/v1/consentimientos",
        "/v1/consentimientos/{consentimientoId}",
        "/v1/consentimientos/texto-vigente",
    ):
        assert ruta in rutas
    lista = rutas["/v1/consentimientos"]["get"]["responses"]["200"]["content"]
    assert lista["application/json"]["schema"]["items"] == {
        "$ref": "#/components/schemas/ConsentimientoDto"
    }
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


def test_el_bff_es_compatible_con_lo_que_clientes_publica() -> None:
    """Las dos rutas nuevas reenvían a `clientes`: sus esquemas de respuesta tienen los mismos
    campos obligatorios en ambos contratos."""
    clientes = yaml.safe_load((CONTRATO.parent / "clientes.yaml").read_text(encoding="utf-8"))[
        "components"
    ]["schemas"]
    propios = app.openapi()["components"]["schemas"]
    for nombre in ("ConsentimientoDto", "HistorialDto", "HitoDto"):
        assert set(propios[nombre]["required"]) == set(clientes[nombre]["required"])
    assert propios["TipoHito"]["enum"] == clientes["TipoHito"]["enum"]
