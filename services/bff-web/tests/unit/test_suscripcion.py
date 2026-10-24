"""UT-W13-05: rutas de suscripción del portal (HU-W13) con polizas y clientes simulados.

Autorización por grupo (asesores solicita y lee; suscriptores lee, ve la bandeja y el resumen),
rol hacia polizas según el grupo, filtrado DS2-07, composición del nombre del cliente con falla de
clientes, errores B2 que pasan y falla cerrada (503) para todo lo demás. Desde HU-W14
(UT-W14-02), la comunicación: pasa tal cual, sin nada reservado ni códigos de reglas.
"""

import copy
import json
import re
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import suscripciones as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.suscripcion import Suscripciones
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
CLAVE = "00000000-0000-4000-c000-000000000001"
DECISION_ID = "00000000-0000-4000-e000-000000000001"
COTIZACION_ID = "00000000-0000-4000-a000-000000000004"
CLIENTE_ID = "00000000-0000-4000-8000-00000000000e"


def _usuario(grupos: str, sub: str = "sub-usuaria") -> dict[str, str]:
    return {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Authenticated-Sub": sub,
        "X-Correlation-Id": "corr-bff-w13",
    }


ASESOR = _usuario("[asesores]", "sub-asesor")
SUSCRIPTOR = _usuario("[suscriptores]", "sub-suscriptora")
AMBOS = _usuario("[asesores, suscriptores]")
OPERACION = _usuario("[operacion]")


def _regla(codigo: str, orden: int, **cambios: Any) -> dict[str, Any]:
    return {
        "codigo": codigo,
        "orden": orden,
        "tipo": "EXCLUYENTE",
        "descripcion": "regla.x",
        "campo": "edad",
        "operador": "entre",
        "valor": ["18", "65"],
        "valorCaso": "36",
        "resultado": "CUMPLE",
        "visibilidad": "EXPLICABLE",
        "reservada": False,
        **cambios,
    }


DECISION: dict[str, Any] = {
    "id": DECISION_ID,
    "codigo": "DEC-2026-000001",
    "cotizacionId": COTIZACION_ID,
    "ofertaId": None,
    "cotizacionCodigo": None,
    "clienteId": CLIENTE_ID,
    "productoId": "vida-hipotecario",
    "resultado": "RECHAZADA",
    "tipo": "AUTOMATICA",
    "reglasEvaluadas": [
        _regla("SUS-001", 1),
        _regla(
            "SUS-015",
            5,
            campo="coincidencias_listas_restrictivas",
            operador="==",
            valor="0",
            valorCaso="1",
            resultado="NO_CUMPLE",
            visibilidad="RESERVADA",
        ),
    ],
    "reglaDeterminante": "SUS-015",
    "motivos": ["NO_ELEGIBLE"],
    "versionReglas": "2026.10.1",
    "hashReglas": "sha256:reglas",
    "condiciones": {
        "socioId": None,
        "versionProducto": "2026.09.1",
        "moneda": "COP",
        "periodicidad": "MENSUAL",
        "primaTotal": "93366",
        "versionReglasPrima": "2026.09.1",
        "huellaCalculo": "sha256:calculo",
        "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
        "sumaAsegurada": "180000000",
        "plazoCreditoMeses": 180,
        "calculadoEn": "2026-10-22T14:00:00Z",
        "vigenteHasta": "2026-10-26T14:00:00Z",
    },
    "contexto": {
        "nivelRiesgo": "ALTO",
        "confianza": "0.92",
        "fuentesConsultadas": 4,
        "fuentesDeclaradas": 4,
    },
    "admiteExcepcion": False,
    "decididaEn": "2026-10-22T15:00:00Z",
    "duracionMs": 41,
}
CASO = {
    "decisionId": DECISION_ID,
    "codigo": "DEC-2026-000002",
    "cotizacionId": COTIZACION_ID,
    "cotizacionCodigo": None,
    "clienteId": CLIENTE_ID,
    "productoId": "vida-hipotecario",
    "moneda": "COP",
    "sumaAsegurada": "620000000",
    "motivos": ["SUMA_REQUIERE_REVISION"],
    "reglasNoCumplidas": ["SUS-004"],
    "decididaEn": "2026-10-22T06:00:00Z",
    "antiguedadMinutos": 540,
}
BANDEJA = {
    "items": [CASO, {**CASO, "codigo": "DEC-2026-000003"}],
    "siguienteCursor": None,
    "total": 2,
}
RESUMEN = {
    "desde": "2026-10-22T05:00:00Z",
    "hasta": "2026-10-23T05:00:00Z",
    "resueltasAutomaticamente": 2,
    "enviadasARevision": 1,
    "total": 3,
    "tiempoMedioDecisionMs": 40,
    "fueraDelCompromiso": 1,
    "compromisoRevisionHoras": 8,
}
CLIENTE = {"id": CLIENTE_ID, "nombreCompleto": "Cliente Sintético 14"}


class Simulados:
    """polizas y clientes simulados; `polizas` y `clientes` deciden cada respuesta."""

    def __init__(self) -> None:
        self.a_polizas: list[httpx.Request] = []
        self.a_clientes: list[httpx.Request] = []
        self.polizas: Callable[[httpx.Request], httpx.Response] = self._polizas_nominal
        self.clientes: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(
            200, json=CLIENTE
        )

    def _polizas_nominal(self, pedido: httpx.Request) -> httpx.Response:
        ruta_ = pedido.url.path
        if pedido.method == "POST":
            return httpx.Response(201, json=DECISION)
        if ruta_.endswith("/bandeja"):
            return httpx.Response(200, json=BANDEJA)
        if ruta_.endswith("/resumen"):
            return httpx.Response(200, json=RESUMEN)
        if ruta_ == "/v1/suscripciones":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json=copy.deepcopy(DECISION))

    def responder_polizas(self, pedido: httpx.Request) -> httpx.Response:
        self.a_polizas.append(pedido)
        return self.polizas(pedido)

    def responder_clientes(self, pedido: httpx.Request) -> httpx.Response:
        self.a_clientes.append(pedido)
        return self.clientes(pedido)


@pytest.fixture
def simulados(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulados]:
    sim = Simulados()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    polizas = PolizasCliente(
        "http://polizas:8080", 1500, httpx.MockTransport(sim.responder_polizas)
    )
    clientes = ClientesCliente(
        "http://clientes:8080", 5000, httpx.MockTransport(sim.responder_clientes)
    )
    app.dependency_overrides[ruta.suscripciones] = lambda: Suscripciones(polizas, clientes, 200)
    yield sim
    app.dependency_overrides.clear()


def _solicitar(encabezados: dict[str, str], cuerpo: Any = None) -> httpx.Response:
    return TestClient(app).post(
        "/v1/suscripciones",
        json=cuerpo or {"cotizacionId": COTIZACION_ID},
        headers={**encabezados, "Idempotency-Key": CLAVE},
    )


def test_solicitar_como_asesor(simulados: Simulados) -> None:
    respuesta = _solicitar(ASESOR)
    assert respuesta.status_code == 201
    [enviada] = simulados.a_polizas
    assert (enviada.method, enviada.url.path) == ("POST", "/v1/suscripciones")
    assert (enviada.headers["X-Actor-Id"], enviada.headers["X-Actor-Rol"]) == (
        "sub-asesor",
        "ASESOR",
    )
    assert enviada.headers["Idempotency-Key"] == CLAVE
    assert enviada.headers["X-Correlation-Id"] == "corr-bff-w13"
    # DS2-07: aunque polizas no la filtre, el asesor no ve el valor de la regla reservada.
    [sus015] = [r for r in respuesta.json()["reglasEvaluadas"] if r["codigo"] == "SUS-015"]
    assert (sus015["valorCaso"], sus015["reservada"]) == (None, True)


def test_repeticion_devuelve_200(simulados: Simulados) -> None:
    simulados.polizas = lambda _: httpx.Response(200, json=DECISION)
    assert _solicitar(ASESOR).status_code == 200


@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        (SUSCRIPTOR, 403, "ROL_NO_AUTORIZADO"),
        (OPERACION, 403, "ROL_NO_AUTORIZADO"),
        ({**ASESOR, "X-Authenticated-Issuer": "https://otro"}, 403, "ROL_NO_AUTORIZADO"),
        ({k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}, 401, "ACTOR_REQUERIDO"),
    ],
)
def test_autorizacion_para_solicitar(
    simulados: Simulados, encabezados: dict[str, str], estado: int, codigo: str
) -> None:
    respuesta = _solicitar(encabezados)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert simulados.a_polizas == []


@pytest.mark.parametrize(
    ("estado", "cuerpo"),
    [
        (
            409,
            {
                "codigo": "OFERTA_VENCIDA",
                "mensaje": "x",
                "detalles": {"accion": "NUEVA_COTIZACION"},
            },
        ),
        (403, {"codigo": "IDENTIDAD_NO_VERIFICADA", "mensaje": "x", "detalles": {}}),
        (409, {"codigo": "IDENTIDAD_PENDIENTE", "mensaje": "x", "detalles": {}}),
        (404, {"codigo": "COTIZACION_NO_ENCONTRADA", "mensaje": "x", "detalles": {}}),
        (422, {"codigo": "IDEMPOTENCIA_CONFLICTO", "mensaje": "x", "detalles": {}}),
        (422, {"codigo": "REGLAS_NO_VIGENTES", "mensaje": "x", "detalles": {}}),
        (
            503,
            {
                "codigo": "DEPENDENCIA_NO_DISPONIBLE",
                "mensaje": "x",
                "detalles": {"dependencia": "catalogo"},
            },
        ),
    ],
)
def test_los_errores_prometidos_pasan(
    simulados: Simulados, estado: int, cuerpo: dict[str, Any]
) -> None:
    simulados.polizas = lambda _: httpx.Response(estado, json=cuerpo)
    respuesta = _solicitar(ASESOR)
    assert respuesta.status_code == estado
    assert (respuesta.json()["codigo"], respuesta.json()["detalles"]) == (
        cuerpo["codigo"],
        cuerpo["detalles"],
    )
    assert respuesta.json()["correlationId"] == "corr-bff-w13"


@pytest.mark.parametrize(
    "respuesta_polizas",
    [
        httpx.Response(500, json={"codigo": "ERROR_INTERNO"}),
        httpx.Response(418, json={"codigo": "OTRO_CODIGO"}),
        httpx.Response(404, text="sin cuerpo B2"),
        httpx.Response(201, json={"id": "fuera-del-contrato"}),
        httpx.Response(201, text="no es json"),
        httpx.ConnectError("caído"),
    ],
)
def test_falla_cerrada(simulados: Simulados, respuesta_polizas: Any) -> None:
    def responder(pedido: httpx.Request) -> httpx.Response:
        if isinstance(respuesta_polizas, Exception):
            raise respuesta_polizas
        return respuesta_polizas

    simulados.polizas = responder
    respuesta = _solicitar(ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")
    assert respuesta.json()["detalles"] == {}


def test_decision_para_el_suscriptor_con_su_nombre(simulados: Simulados) -> None:
    respuesta = TestClient(app).get(f"/v1/suscripciones/{DECISION_ID}", headers=SUSCRIPTOR)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["clienteNombre"] == "Cliente Sintético 14"
    [sus015] = [r for r in cuerpo["reglasEvaluadas"] if r["codigo"] == "SUS-015"]
    assert (sus015["valorCaso"], sus015["reservada"]) == ("1", False)
    [a_polizas] = simulados.a_polizas
    assert a_polizas.headers["X-Actor-Rol"] == "SUSCRIPTOR"
    [a_clientes] = simulados.a_clientes
    assert a_clientes.url.path == f"/v1/clientes/{CLIENTE_ID}"


@pytest.mark.parametrize(("encabezados", "rol"), [(ASESOR, "ASESOR"), (AMBOS, "SUSCRIPTOR")])
def test_el_rol_hacia_polizas_sale_del_grupo(
    simulados: Simulados, encabezados: dict[str, str], rol: str
) -> None:
    cuerpo = TestClient(app).get(f"/v1/suscripciones/{DECISION_ID}", headers=encabezados).json()
    assert simulados.a_polizas[0].headers["X-Actor-Rol"] == rol
    [sus015] = [r for r in cuerpo["reglasEvaluadas"] if r["codigo"] == "SUS-015"]
    assert sus015["reservada"] is (rol == "ASESOR")


@pytest.mark.parametrize(
    "falla_clientes",
    [
        lambda _: httpx.Response(503, json={"codigo": "X"}),
        lambda _: httpx.Response(200, json={"id": CLIENTE_ID}),
        lambda _: httpx.Response(200, json=["no", "objeto"]),
        lambda _: httpx.Response(200, text="no es json"),
    ],
)
def test_sin_nombre_si_clientes_falla(simulados: Simulados, falla_clientes: Any) -> None:
    simulados.clientes = falla_clientes
    respuesta = TestClient(app).get(f"/v1/suscripciones/{DECISION_ID}", headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json()["clienteNombre"] is None


def test_sin_nombre_si_clientes_tarda(simulados: Simulados) -> None:
    def lento(pedido: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=pedido)

    simulados.clientes = lento
    cuerpo = TestClient(app).get("/v1/bandeja-suscripcion", headers=SUSCRIPTOR).json()
    assert [c["clienteNombre"] for c in cuerpo["items"]] == [None, None]


def test_decision_inexistente_y_otros_grupos(simulados: Simulados) -> None:
    simulados.polizas = lambda _: httpx.Response(
        404, json={"codigo": "DECISION_NO_ENCONTRADA", "mensaje": "x", "detalles": {}}
    )
    no_existe = TestClient(app).get(f"/v1/suscripciones/{DECISION_ID}", headers=ASESOR)
    assert (no_existe.status_code, no_existe.json()["codigo"]) == (404, "DECISION_NO_ENCONTRADA")
    otro = TestClient(app).get(f"/v1/suscripciones/{DECISION_ID}", headers=OPERACION)
    assert otro.status_code == 403


def test_buscar_por_cotizacion_solo_asesores(simulados: Simulados) -> None:
    cliente = TestClient(app)
    respuesta = cliente.get(
        "/v1/suscripciones", params={"cotizacionId": COTIZACION_ID}, headers=ASESOR
    )
    assert (respuesta.status_code, respuesta.json()) == (200, {"items": []})
    assert simulados.a_polizas[0].url.params["cotizacionId"] == COTIZACION_ID
    suscriptor = cliente.get(
        "/v1/suscripciones", params={"cotizacionId": COTIZACION_ID}, headers=SUSCRIPTOR
    )
    assert suscriptor.status_code == 403


def test_bandeja_con_nombres_compuestos_una_vez_por_cliente(simulados: Simulados) -> None:
    cliente = TestClient(app)
    respuesta = cliente.get(
        "/v1/bandeja-suscripcion",
        params={"motivo": "SUMA_REQUIERE_REVISION", "limite": 5, "q": "COT-0000"},
        headers=SUSCRIPTOR,
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert [c["clienteNombre"] for c in cuerpo["items"]] == ["Cliente Sintético 14"] * 2
    assert cuerpo["total"] == 2
    [a_polizas] = simulados.a_polizas
    assert a_polizas.url.path == "/v1/suscripciones/bandeja"
    assert a_polizas.headers["X-Actor-Rol"] == "SUSCRIPTOR"
    assert dict(a_polizas.url.params) == {
        "motivo": "SUMA_REQUIERE_REVISION",
        "q": "COT-0000",
        "limite": "5",
    }
    assert len(simulados.a_clientes) == 1  # el mismo cliente se pide una sola vez
    assert cliente.get("/v1/bandeja-suscripcion", headers=ASESOR).status_code == 403


def test_bandeja_fuera_del_contrato(simulados: Simulados) -> None:
    simulados.polizas = lambda _: httpx.Response(200, json={"items": "no-es-lista"})
    respuesta = TestClient(app).get("/v1/bandeja-suscripcion", headers=SUSCRIPTOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")


def test_resumen(simulados: Simulados) -> None:
    cliente = TestClient(app)
    dia = {"desde": "2026-10-22T05:00:00Z", "hasta": "2026-10-23T05:00:00Z"}
    respuesta = cliente.get("/v1/bandeja-suscripcion/resumen", params=dia, headers=SUSCRIPTOR)
    assert (respuesta.status_code, respuesta.json()) == (200, RESUMEN)
    enviados = dict(simulados.a_polizas[0].url.params)
    assert enviados == {"desde": "2026-10-22T05:00:00+00:00", "hasta": "2026-10-23T05:00:00+00:00"}
    simulados.polizas = lambda _: httpx.Response(
        422,
        json={"codigo": "SOLICITUD_INVALIDA", "mensaje": "x", "detalles": {"campos": ["desde"]}},
    )
    invertido = cliente.get("/v1/bandeja-suscripcion/resumen", params=dia, headers=SUSCRIPTOR)
    assert (invertido.status_code, invertido.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    sin_zona = cliente.get(
        "/v1/bandeja-suscripcion/resumen",
        params={"desde": "2026-10-22T00:00:00", "hasta": dia["hasta"]},
        headers=SUSCRIPTOR,
    )
    assert sin_zona.status_code == 422


@pytest.mark.parametrize("ruta_", ["/v1/bandeja-suscripcion", "/v1/bandeja-suscripcion/resumen"])
def test_rechazos_de_polizas_en_bandeja_y_resumen(simulados: Simulados, ruta_: str) -> None:
    simulados.polizas = lambda _: httpx.Response(
        422, json={"codigo": "SOLICITUD_INVALIDA", "mensaje": "x", "detalles": {}}
    )
    parametros = {"desde": "2026-10-22T05:00:00Z", "hasta": "2026-10-23T05:00:00Z"}
    respuesta = TestClient(app).get(ruta_, params=parametros, headers=SUSCRIPTOR)
    assert respuesta.status_code == 422


# --- HU-W14: comunicación (UT-W14-02) --------------------------------------------------------

COMUNICACION_RECHAZO: dict[str, Any] = {
    "condicion": "NO_ASEGURABLE",
    "admiteContratacion": False,
    "siguientePaso": "COTIZAR_ALTERNATIVA",
    "vigenteHasta": None,
    "motivoCliente": "NO_ELEGIBLE",
    "alternativas": [
        {"productoId": "vida-senior", "descriptor": {"codigo": "EDAD_MAXIMA", "valor": "80"}},
        {
            "productoId": "proteccion-pagos",
            "descriptor": {"codigo": "SIN_LIMITE_EDAD", "valor": None},
        },
    ],
    "plazoRespuestaHoras": None,
}
CODIGO_REGLA = re.compile(r"SUS-\d{3}")


def _con_comunicacion(comunicacion: Any) -> Callable[[httpx.Request], httpx.Response]:
    def responder(pedido: httpx.Request) -> httpx.Response:
        estado = 201 if pedido.method == "POST" else 200
        return httpx.Response(
            estado, json={**copy.deepcopy(DECISION), "comunicacion": comunicacion}
        )

    return responder


def _post_y_get(encabezados: dict[str, str]) -> list[httpx.Response]:
    leida = TestClient(app).get(f"/v1/suscripciones/{DECISION_ID}", headers=encabezados)
    return [_solicitar(encabezados), leida]


def test_hu_w14_la_comunicacion_llega_al_asesor_sin_nada_reservado(simulados: Simulados) -> None:
    """DS2-07 y R-08: el asesor recibe la comunicación tal cual; ni en ella ni en las reglas
    aparece el valor de SUS-015, y la comunicación no lleva códigos de reglas."""
    simulados.polizas = _con_comunicacion(COMUNICACION_RECHAZO)
    for respuesta in _post_y_get(ASESOR):
        assert respuesta.status_code in (200, 201)
        cuerpo = respuesta.json()
        assert cuerpo["comunicacion"] == COMUNICACION_RECHAZO
        assert not CODIGO_REGLA.search(json.dumps(cuerpo["comunicacion"]))
        reservadas = [r for r in cuerpo["reglasEvaluadas"] if r["visibilidad"] == "RESERVADA"]
        assert [(r["valorCaso"], r["reservada"]) for r in reservadas] == [(None, True)]


def test_hu_w14_el_suscriptor_recibe_la_misma_comunicacion(simulados: Simulados) -> None:
    simulados.polizas = _con_comunicacion(COMUNICACION_RECHAZO)
    cuerpo = TestClient(app).get(f"/v1/suscripciones/{DECISION_ID}", headers=SUSCRIPTOR).json()
    assert cuerpo["comunicacion"] == COMUNICACION_RECHAZO


@pytest.mark.parametrize(
    "fuga",
    [
        {"motivoCliente": "SUS-015"},
        {"condicion": "RECHAZADA_POR_LISTAS"},
        {"alternativas": [{"productoId": "SUS-015", "descriptor": {"codigo": "EDAD_MAXIMA"}}]},
        {"alternativas": [{"productoId": "vida-senior", "descriptor": {"codigo": "LISTAS"}}]},
        {"admiteContratacion": "quizas"},
    ],
)
def test_hu_w14_una_comunicacion_fuera_del_contrato_es_falla_cerrada(
    simulados: Simulados, fuga: dict[str, Any]
) -> None:
    """Todo campo de la comunicación es un conjunto cerrado: un código de regla no cabe."""
    simulados.polizas = _con_comunicacion({**COMUNICACION_RECHAZO, **fuga})
    for respuesta in _post_y_get(ASESOR):
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (
            503,
            "SERVICIO_NO_DISPONIBLE",
        )


def test_hu_w14_un_campo_extra_en_la_comunicacion_no_llega_al_portal(
    simulados: Simulados,
) -> None:
    simulados.polizas = _con_comunicacion({**COMUNICACION_RECHAZO, "regla": "SUS-015"})
    for respuesta in _post_y_get(ASESOR):
        assert "regla" not in respuesta.json()["comunicacion"]
        assert not CODIGO_REGLA.search(json.dumps(respuesta.json()["comunicacion"]))


def test_hu_w14_una_decision_sin_comunicacion_sigue_pasando(simulados: Simulados) -> None:
    """Criterio técnico 5: polizas de la versión W13 (sin el campo) no rompe el portal."""
    for respuesta in _post_y_get(ASESOR):
        assert respuesta.status_code in (200, 201)
        assert respuesta.json()["comunicacion"] is None
