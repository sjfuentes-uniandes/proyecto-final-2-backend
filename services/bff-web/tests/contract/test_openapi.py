"""El contrato publicado para el frontend (contracts/openapi/bff-web.yaml) es el que sirve la app.

Si cambia la API, regenerar con:
    uv run python -c "import yaml; from bff_web.main import app; \
open('contracts/openapi/bff-web.yaml', 'w').write(yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True))"
y sincronizar el frontend con scripts/sync-contracts.sh <ref>.
"""

from pathlib import Path

import yaml
from bff_web.main import app

CONTRATO = Path(__file__).parents[4] / "contracts" / "openapi" / "bff-web.yaml"


def test_contrato_publicado_coincide_con_la_app() -> None:
    assert yaml.safe_load(CONTRATO.read_text(encoding="utf-8")) == app.openapi()


def test_contrato_de_trazas() -> None:
    operacion = app.openapi()["paths"]["/operacion/trazas"]["get"]
    assert {p["name"] for p in operacion["parameters"] if p["in"] == "query"} == {
        "correlation_id",
        "recorrido",
        "duracion_min_ms",
        "horas",
    }
    assert set(operacion["responses"]) >= {"200", "422"}


def test_contrato_de_alertas() -> None:
    operacion = app.openapi()["paths"]["/operacion/alertas"]["get"]
    esquemas = app.openapi()["components"]["schemas"]
    assert set(operacion["responses"]) >= {"200"}
    assert set(esquemas["AlertasRespuesta"]["properties"]) == {
        "ambiente",
        "porcentaje_advertencia",
        "falso_positivo_min",
        "atencion_min",
        "abiertas",
        "umbrales",
        "resumen",
    }
    assert esquemas["AlertaRespuesta"]["properties"]["severidad"]["enum"] == [
        "critica",
        "advertencia",
        "informativa",
    ]
    assert esquemas["EnlaceRespuesta"]["properties"]["tipo"]["enum"] == [
        "traza",
        "socio",
        "cola",
        "tablero",
    ]


COTIZACION = CONTRATO.parent / "cotizacion.yaml"
# HU-W11: el BFF publica los mismos esquemas que cotizacion para /v1/perfiles (04 W11-P15).
ESQUEMAS_PERFILAMIENTO = (
    "SolicitudPerfil",
    "DatosDeclaradosDto",
    "PerfilRespuesta",
    "ResumenPerfil",
    "ListaPerfiles",
    "FactorDto",
    "AjusteDto",
    "DatoRequeridoDto",
    "ConsentimientoDto",
    "FuenteDto",
    "AccionSugerida",
    "DecisionConsentimiento",
    "Direccion",
    "EstadoFactor",
    "EstadoFuente",
    "EstadoPerfil",
    "Fuente",
    "MotivoDato",
    "NivelRiesgo",
    "TipoFuente",
)


def test_dto_de_perfilamiento_iguales_a_cotizacion() -> None:
    propios = app.openapi()["components"]["schemas"]
    de_cotizacion = yaml.safe_load(COTIZACION.read_text(encoding="utf-8"))["components"]["schemas"]
    for nombre in ESQUEMAS_PERFILAMIENTO:
        assert propios[nombre] == de_cotizacion[nombre], nombre


# HU-W03: el BFF publica los mismos esquemas que cotizacion para /v1/cotizaciones y las
# condiciones del producto.
ESQUEMAS_COTIZACION = (
    "SolicitudCotizacion",
    "DatosAseguradoDto",
    "CotizacionRespuesta",
    "CoberturaProductoDto",
    "FactorPrimaDto",
    "RecargoPrimaDto",
    "DesglosePrimaDto",
    "EntradasPrimaDto",
    "PasoCotizacionDto",
    "OrigenDato",
    "EstadoCotizacion",
    "CondicionesProducto",
    "LimitesDto",
    "RangoDto",
    # HU-W04
    "OfertaDto",
    "CoberturaOfertaDto",
    "ExclusionDto",
    "EstadoOferta",
    # HU-W06
    "FaltanteDto",
    "ReglaAplicadaDto",
    "MotivoFalla",
)


def test_dto_de_cotizacion_iguales_a_cotizacion() -> None:
    propios = app.openapi()["components"]["schemas"]
    de_cotizacion = yaml.safe_load(COTIZACION.read_text(encoding="utf-8"))["components"]["schemas"]
    for nombre in ESQUEMAS_COTIZACION:
        assert propios[nombre] == de_cotizacion[nombre], nombre


def test_contrato_de_cotizaciones() -> None:
    rutas = app.openapi()["paths"]
    cotizar = rutas["/v1/cotizaciones"]["post"]
    assert set(cotizar["responses"]) == {"200", "201", "401", "403", "404", "422", "503"}
    encabezados = {p["name"] for p in cotizar["parameters"] if p["in"] == "header"}
    assert "Idempotency-Key" in encabezados
    leer = rutas["/v1/cotizaciones/{cotizacionId}"]["get"]
    assert set(leer["responses"]) == {"200", "401", "403", "404", "422", "503"}
    condiciones = rutas["/v1/productos/{productoId}/condiciones"]["get"]
    assert set(condiciones["responses"]) == {"200", "401", "403", "404", "422", "503"}


CLIENTES = CONTRATO.parent / "clientes.yaml"
# HU-W30: el BFF publica los mismos esquemas que clientes para /v1/clientes.
ESQUEMAS_CLIENTES = ("SolicitudAltaCliente", "ClienteDto", "EstadoIdentidad")


def test_dto_de_clientes_iguales_a_clientes() -> None:
    propios = app.openapi()["components"]["schemas"]
    de_clientes = yaml.safe_load(CLIENTES.read_text(encoding="utf-8"))["components"]["schemas"]
    for nombre in ESQUEMAS_CLIENTES:
        assert propios[nombre] == de_clientes[nombre], nombre


def test_contrato_de_clientes() -> None:
    rutas = app.openapi()["paths"]
    alta = rutas["/v1/clientes"]["post"]
    assert set(alta["responses"]) == {"200", "201", "401", "403", "422", "503"}
    assert set(rutas["/v1/clientes/{clienteId}"]["get"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "422",
        "503",
    }
    verificar = rutas["/v1/clientes/{clienteId}/identidad/verificaciones"]["post"]
    assert set(verificar["responses"]) == {"200", "401", "403", "404", "409", "422", "503"}
    # Sin PII ni motivo del proveedor en la respuesta.
    assert set(app.openapi()["components"]["schemas"]["ClienteDto"]["properties"]) == {
        "clienteId",
        "estado",
        "verificadoEn",
        "proveedor",
        "referencia",
    }


# HU-W31 (PR 3.5): lectura de consentimientos para el asesor, con los esquemas de clientes.
# `ConsentimientoDto` ya es el de cotizacion (dentro del perfil): el de clientes se publica como
# `ConsentimientoClienteDto`, igual salvo el título.
ESQUEMAS_CONSENTIMIENTO = ("HistorialDto", "HitoDto", "EstadoConsentimiento", "TipoHito")
LISTA_CONSENTIMIENTOS = "/v1/clientes/{clienteId}/consentimientos"
HISTORIAL_CONSENTIMIENTO = "/v1/clientes/{clienteId}/consentimientos/{consentimientoId}/historial"


def _sin_titulo(esquema: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in esquema.items() if k != "title"}


def test_dto_de_consentimientos_iguales_a_clientes() -> None:
    propios = app.openapi()["components"]["schemas"]
    de_clientes = yaml.safe_load(CLIENTES.read_text(encoding="utf-8"))["components"]["schemas"]
    for nombre in ESQUEMAS_CONSENTIMIENTO:
        assert propios[nombre] == de_clientes[nombre], nombre
    assert _sin_titulo(propios["ConsentimientoClienteDto"]) == _sin_titulo(
        de_clientes["ConsentimientoDto"]
    )


def test_contrato_de_consentimientos_solo_lectura() -> None:
    """ADR 0002: solo GET; mismas rutas que clientes; el ConsentimientoDto del perfil no cambia."""
    rutas = app.openapi()["paths"]
    assert set(rutas[LISTA_CONSENTIMIENTOS]) == {"get"}
    assert set(rutas[HISTORIAL_CONSENTIMIENTO]) == {"get"}
    lista = rutas[LISTA_CONSENTIMIENTOS]["get"]
    historial = rutas[HISTORIAL_CONSENTIMIENTO]["get"]
    for operacion in (lista, historial):
        assert set(operacion["responses"]) == {"200", "401", "403", "404", "422", "503"}
        assert "requestBody" not in operacion
    esquema_lista = lista["responses"]["200"]["content"]["application/json"]["schema"]
    assert esquema_lista["items"] == {"$ref": "#/components/schemas/ConsentimientoClienteDto"}
    esquema_historial = historial["responses"]["200"]["content"]["application/json"]["schema"]
    assert esquema_historial == {"$ref": "#/components/schemas/HistorialDto"}
    de_clientes = yaml.safe_load(CLIENTES.read_text(encoding="utf-8"))["paths"]
    assert {LISTA_CONSENTIMIENTOS, HISTORIAL_CONSENTIMIENTO} <= set(de_clientes)
    perfil = app.openapi()["components"]["schemas"]["ConsentimientoDto"]
    assert set(perfil["properties"]) == {"decision", "causa", "vigenteHasta"}


def test_contrato_de_perfiles() -> None:
    rutas = app.openapi()["paths"]
    crear = rutas["/v1/perfiles"]["post"]
    assert set(crear["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    requeridos = {p["name"] for p in crear["parameters"] if p["required"]}
    assert requeridos == {"Idempotency-Key"}
    assert set(rutas["/v1/perfiles/{perfilId}"]["get"]["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "422",
        "503",
    }
    consulta = {p["name"] for p in rutas["/v1/perfiles"]["get"]["parameters"] if p["in"] == "query"}
    assert consulta == {"clienteId", "limite"}
    # HU-W12: recálculo y comparación (05 §B.4.2).
    recalculo = rutas["/v1/perfiles/{perfilId}/recalculos"]["post"]
    assert set(recalculo["responses"]) == {"200", "201", "401", "403", "404", "409", "422", "503"}
    assert {p["name"] for p in recalculo["parameters"] if p["required"]} == {
        "perfilId",
        "Idempotency-Key",
    }
    assert "requestBody" not in recalculo
    comparacion = rutas["/v1/perfiles/{perfilId}/comparacion"]["get"]
    assert set(comparacion["responses"]) == {"200", "401", "403", "404", "422", "503"}
    assert app.openapi()["components"]["schemas"]["ComparacionPerfiles"]["required"] == [
        "anterior",
        "actual",
    ]


def test_hu_w31_la_marca_de_revocacion_es_opcional_en_el_bff() -> None:
    """El BFF transmite la marca de cotizacion como campos opcionales (aditivo para la web)."""
    perfil = app.openapi()["components"]["schemas"]["PerfilRespuesta"]
    nuevos = {"comprobacionFinal", "revocadoDurantePerfilamiento"}
    assert nuevos <= set(perfil["properties"])
    assert not nuevos & set(perfil["required"])
    assert len(perfil["required"]) == 20


POLIZAS = CONTRATO.parent / "polizas.yaml"
# HU-W13: el BFF publica los mismos esquemas que polizas; las vistas agregan clienteNombre.
# HU-W14: también los de la comunicación (DecisionSuscripcionVista hereda `comunicacion`).
ESQUEMAS_SUSCRIPCION = (
    "SolicitudSuscripcion",
    "DecisionSuscripcion",
    "ReglaEvaluadaDto",
    "CondicionesOfertaDto",
    "ContextoCasoDto",
    "ResumenDecision",
    "ListaDecisiones",
    "ResumenBandeja",
    "ResultadoDecision",
    "TipoDecision",
    "TipoRegla",
    "CampoRegla",
    "OperadorRegla",
    "Visibilidad",
    "ResultadoRegla",
    "MotivoCliente",
    "CoberturaProductoDto",
    "NivelRiesgo",
    # HU-W14: comunicación de la decisión.
    "ComunicacionDto",
    "AlternativaDto",
    "DescriptorAlternativaDto",
    "CondicionAseguramiento",
    "SiguientePaso",
    "CodigoDescriptor",
)


def test_dto_de_suscripcion_iguales_a_polizas() -> None:
    propios = app.openapi()["components"]["schemas"]
    de_polizas = yaml.safe_load(POLIZAS.read_text(encoding="utf-8"))["components"]["schemas"]
    for nombre in ESQUEMAS_SUSCRIPCION:
        assert propios[nombre] == de_polizas[nombre], nombre
    for vista, base in (
        ("DecisionSuscripcionVista", "DecisionSuscripcion"),
        ("CasoBandejaVista", "CasoBandeja"),
    ):
        extra = set(propios[vista]["properties"]) - set(de_polizas[base]["properties"])
        assert extra == {"clienteNombre"}, vista


def test_contrato_de_suscripciones() -> None:
    rutas = app.openapi()["paths"]
    assert set(rutas["/v1/suscripciones"]) == {"get", "post"}
    assert set(rutas["/v1/suscripciones"]["post"]["responses"]) == {
        "200",
        "201",
        "401",
        "403",
        "404",
        "409",
        "422",
        "503",
    }
    assert set(rutas["/v1/bandeja-suscripcion"]) == {"get"}
    assert set(rutas["/v1/bandeja-suscripcion/resumen"]) == {"get"}


# HU-W15: aceptación de la oferta; DTO copiados de polizas.
ESQUEMAS_ACEPTACION = (
    "SolicitudAceptacion",
    "DeclaracionesDto",
    "Aceptacion",
    "CanalAceptacion",
    "SiguientePasoContratacion",
)
ACEPTACION = "/v1/cotizaciones/{cotizacionId}/aceptacion"


def test_dto_de_aceptacion_iguales_a_polizas() -> None:
    propios = app.openapi()["components"]["schemas"]
    de_polizas = yaml.safe_load(POLIZAS.read_text(encoding="utf-8"))["components"]["schemas"]
    for nombre in ESQUEMAS_ACEPTACION:
        assert propios[nombre] == de_polizas[nombre], nombre


def test_contrato_de_aceptacion() -> None:
    ruta = app.openapi()["paths"][ACEPTACION]
    assert set(ruta) == {"get", "post"}
    registrar = ruta["post"]
    assert set(registrar["responses"]) == {
        "200",
        "201",
        "401",
        "403",
        "404",
        "409",
        "422",
        "503",
    }
    assert {p["name"] for p in registrar["parameters"] if p["required"]} == {
        "cotizacionId",
        "Idempotency-Key",
    }
    assert set(ruta["get"]["responses"]) == {"200", "401", "403", "404", "422", "503"}
