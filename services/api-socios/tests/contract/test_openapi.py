"""Regenerar el contrato con:
    uv run python -c "import yaml; from api_socios.main import app; \
open('contracts/openapi/api-socios.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
"""

from pathlib import Path

import yaml
from api_socios.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "api-socios.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_documenta_los_codigos_de_la_historia() -> None:
    rutas = app.openapi()["paths"]
    assert set(rutas["/admin/socios"]["post"]["responses"]) >= {"201", "400", "422"}
    assert set(rutas["/socio/me"]["get"]["responses"]) >= {"200", "401"}
    assert set(rutas["/polizas"]["post"]["responses"]) >= {"401", "403", "501"}
    # HU-W04: la oferta, sin el 501 de antes.
    assert set(rutas["/cotizaciones"]["post"]["responses"]) == {
        "200",
        "201",
        "400",
        "401",
        "403",
        "404",
        "410",
        "422",
        "503",
    }
    assert set(rutas["/cotizaciones/{cotizacion_id}"]["get"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "410",
        "422",
        "503",
    }
    assert set(rutas["/admin/socios/{socio_id}/plan"]["put"]["responses"]) >= {
        "200",
        "404",
        "409",
        "422",
    }
    assert set(rutas["/admin/socios/{socio_id}/consumo"]["get"]["responses"]) >= {"200", "404"}
    for operaciones in rutas.values():
        for operacion in operaciones.values():
            for codigo, respuesta in operacion["responses"].items():
                if codigo.startswith(("4", "5")):
                    esquema = respuesta["content"]["application/json"]["schema"]["$ref"]
                    assert esquema == "#/components/schemas/ErrorDto", (
                        codigo,
                        operacion["operationId"],
                    )
    consumo = app.openapi()["components"]["schemas"]["ConsumoRespuesta"]["properties"]
    assert not {"apiKey", "apiKeyId", "clientSecret"} & set(consumo)
    creado = app.openapi()["components"]["schemas"]["SocioCreado"]["properties"]
    respuesta = app.openapi()["components"]["schemas"]["SocioRespuesta"]["properties"]
    assert {"clientSecret", "apiKey"} <= set(creado) and not {"clientSecret", "apiKey"} & set(
        respuesta
    )


def test_hu_w04_la_oferta_publica_no_expone_campos_internos() -> None:
    esquemas = app.openapi()["components"]["schemas"]
    oferta = set(esquemas["OfertaV1"]["properties"])
    assert oferta == {
        "id",
        "codigo",
        "cotizacionId",
        "productoId",
        "precio",
        "moneda",
        "periodicidad",
        "coberturas",
        "exclusionesResumidas",
        "vigenteHasta",
        "estado",
        "version",
        "versionReglas",
        "creadaEn",
        # HU-W06: opcionales y aditivos.
        "degradada",
        "faltantes",
    }
    assert "degradada" not in esquemas["OfertaV1"].get("required", [])
    assert "faltantes" not in esquemas["OfertaV1"].get("required", [])
    # Del fallback, el socio solo ve qué dato faltó: ni la regla, ni el motivo, ni un valor.
    assert set(esquemas["DatoFaltanteV1"]["properties"]) == {"fuente", "dato"}
    internos = {"huellaCalculo", "hashReglas", "entradas", "desglose", "perfilId", "socioId"}
    internos |= {"regla", "versionReglasFallback", "valor"}
    for nombre in ("OfertaV1", "CoberturaOfertaV1", "ExclusionResumidaV1", "DatoFaltanteV1"):
        assert not internos & set(esquemas[nombre]["properties"]), nombre
    # La solicitud no admite perfil ni socio: el socio es la credencial.
    solicitud = esquemas["SolicitudCotizacionV1"]
    assert solicitud["additionalProperties"] is False
    assert not {"perfilId", "socioId"} & set(solicitud["properties"])
