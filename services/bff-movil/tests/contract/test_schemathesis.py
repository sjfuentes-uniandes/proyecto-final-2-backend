"""Schemathesis 4 contra la app con una app real que nunca contacta a `clientes`.

Sin un JWT firmado válido, la autenticación siempre rechaza con 401: el objetivo aquí es
que la API nunca responda con un esquema distinto del declarado, no recorrer la autenticación
(cubierta en tests/unit/sesion). Un transporte que falla asegura que, si alguna vez se
alcanzara la llamada a `clientes`, no haya tráfico de red real.
"""

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import schemathesis
from bff_movil.config import Settings
from bff_movil.main import crear_app
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from hypothesis import settings
from schemathesis.specs.openapi.checks import negative_data_rejection

# Starlette solo lista en Allow los métodos de la primera ruta que coincide: en
# /v1/consentimientos, POST y GET son dos rutas y el 405 de una no anuncia la otra.
EXCLUIDOS = [schemathesis.checks.allow_header_conformance]

VERIFICACION_NOMINAL = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "estado": "VERIFICADO",
    "resultado": "APROBADO",
    "motivo": None,
    "verificadoEn": "2026-10-01T00:00:00Z",
    "tokenRegistro": "v1.token-sintetico.firma-sintetica",
    "expiraEn": "2026-10-01T00:30:00Z",
}


# POST /v1/registro/verificaciones es publica: no pasa por la autenticacion y si alcanza a
# `clientes`, que aqui responde una verificacion nominal. Cualquier otra llamada falla.
def _clientes_simulado(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path.endswith("/verificaciones"):
        return httpx.Response(200, json=VERIFICACION_NOMINAL)
    raise httpx.ConnectError("Schemathesis no debe alcanzar a clientes")


app = crear_app(Settings(environment="pruebas"), transport=httpx.MockTransport(_clientes_simulado))
# Arranca el lifespan una vez: crea los clientes HTTP y el caso de uso en app.state.
_ciclo = TestClient(app)
_ciclo.__enter__()

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@schema.parametrize()
@settings(max_examples=30, deadline=None)
def test_api_cumple_su_contrato(case: schemathesis.Case) -> None:
    case.call_and_validate(excluded_checks=EXCLUIDOS)


# --- Consentimientos con un titular autenticado (HU-M08) --------------------------------------
# Con un token válido el barrido pasa del 401 y llega a las rutas: `clientes` simulado responde
# su caso nominal, de modo que se valida lo que el BFF devuelve en 200/201, 404, 422 y 503.

EMISOR = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_schemathesis"
SUB = "55555555-5555-4555-8555-555555555555"
KID = "kid-schemathesis"
CONSENTIMIENTO = {
    "consentimientoId": "00000000-0000-4000-9000-000000000001",
    "estado": "ACTIVO",
    "proposito": "PERFILAMIENTO_PRECIO",
    "alcances": ["FA_PRODUCTOS_VIGENTES"],
    "versionTexto": "2026-10-17.1",
    "idioma": "es-CO",
    "otorgadoEn": "2026-10-17T15:00:00Z",
    "vigenteHasta": "2027-10-17T23:59:59Z",
    "revocadoEn": None,
}
REVOCADO = {**CONSENTIMIENTO, "estado": "REVOCADO", "revocadoEn": "2026-11-02T08:15:00Z"}
HISTORIAL = {
    "consentimientoId": CONSENTIMIENTO["consentimientoId"],
    "estado": "REVOCADO",
    "hitos": [
        {"tipo": "OTORGADO", "ocurridoEn": "2026-10-17T15:00:00Z"},
        {"tipo": "REVOCADO", "ocurridoEn": "2026-11-02T08:15:00Z"},
    ],
}
TEXTO = {
    "proposito": "PERFILAMIENTO_PRECIO",
    "idioma": "es-CO",
    "versionTexto": "2026-10-17.1",
    "titulo": "Autorización de ejemplo",
    "cuerpo": "Texto ilustrativo.",
    "aviso": "Sin validez legal",
    "vigenciasMeses": [6, 12],
    "vigenciaDefectoMeses": 12,
}
_llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_llave.public_key(), as_dict=True)


def _clientes_con_consentimientos(request: httpx.Request) -> httpx.Response:
    ruta = request.url.path
    if ruta.endswith("/.well-known/jwks.json"):
        return httpx.Response(200, json={"keys": [{**_jwk, "kid": KID, "alg": "RS256"}]})
    if ruta == "/v1/consentimientos/textos/vigente":
        return httpx.Response(200, json=TEXTO)
    if ruta == "/v1/clientes/actual/consentimientos":
        if request.method == "POST":
            return httpx.Response(201, json=CONSENTIMIENTO)
        return httpx.Response(200, json=[CONSENTIMIENTO])
    propio = f"/v1/clientes/actual/consentimientos/{CONSENTIMIENTO['consentimientoId']}"
    if ruta == propio:
        return httpx.Response(200, json=CONSENTIMIENTO)
    if ruta == f"{propio}/revocacion":
        return httpx.Response(200, json=REVOCADO)
    if ruta == f"{propio}/historial":
        return httpx.Response(200, json=HISTORIAL)
    if ruta.startswith("/v1/clientes/actual/consentimientos/"):
        return httpx.Response(404, json={"codigo": "CONSENTIMIENTO_NO_ENCONTRADO"})
    raise httpx.ConnectError("ruta de clientes no simulada")


def _token() -> str:
    ahora = datetime.now(UTC)
    claims = {"sub": SUB, "iss": EMISOR, "token_use": "access", "iat": ahora}
    claims["exp"] = ahora + timedelta(hours=2)
    return jwt.encode(claims, _llave, algorithm="RS256", headers={"kid": KID})


app_titular = crear_app(
    Settings(environment="pruebas", JWT_ISSUERS={"customers": EMISOR}),
    transport=httpx.MockTransport(_clientes_con_consentimientos),
)
_ciclo_titular = TestClient(app_titular)
_ciclo_titular.__enter__()
TITULAR = {
    "Authorization": f"Bearer {_token()}",
    "X-Authenticated-Sub": SUB,
    "X-Authenticated-Issuer": EMISOR,
}

schema_titular = schemathesis.openapi.from_asgi("/openapi.json", app_titular)


@schema_titular.include(path_regex="consentimientos").parametrize()
@settings(max_examples=60, deadline=None)
def test_consentimientos_cumplen_su_contrato_con_un_titular(case: schemathesis.Case) -> None:
    excluidos = list(EXCLUIDOS)
    if case.path.endswith("/texto-vigente"):
        # FastAPI ignora los parámetros de consulta desconocidos (`?=true`) en todo el servicio;
        # proposito e idioma los valida `clientes` (422), cubierto en unitarias y de módulo.
        excluidos.append(negative_data_rejection)
    case.call_and_validate(headers=TITULAR, excluded_checks=excluidos)


def _llamar(metodo: str, ruta: str, **kwargs: object) -> httpx.Response:
    """Llama la operación publicada con el titular y valida la respuesta contra su esquema."""
    caso = schema_titular[ruta][metodo].Case(headers=TITULAR, **kwargs)
    respuesta = caso.call()
    caso.validate_response(respuesta, excluded_checks=EXCLUIDOS)
    return respuesta


def test_revocar_e_historial_de_uno_propio_cumplen_el_contrato() -> None:
    """El barrido casi siempre genera identificadores aleatorios (404): aquí se piden las rutas
    con el consentimiento simulado y se validan sus 200 contra el esquema publicado (HU-M09)."""
    ruta = "/v1/consentimientos/{consentimientoId}"
    parametros = {"consentimientoId": CONSENTIMIENTO["consentimientoId"]}

    revocado = _llamar("POST", f"{ruta}/revocacion", path_parameters=parametros)
    historial = _llamar("GET", f"{ruta}/historial", path_parameters=parametros)

    assert (revocado.status_code, historial.status_code) == (200, 200)
    assert revocado.json() == REVOCADO
    assert historial.json() == HISTORIAL
