"""HU-W30: rutas /v1/clientes del portal con `clientes` simulado."""

import json
from collections.abc import Iterator

import httpx
import pytest
from bff_web.adapters.inbound import clientes as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.application.clientes import Clientes
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
    "X-Correlation-Id": "corr-bff-w30",
}
CLIENTE_ID = "00000000-0000-4000-8000-000000000001"
ALTA = {
    "tipoDocumento": "CC",
    "numeroDocumento": "1020456789",
    "nombreCompleto": "Natalia González Ruiz",
    "fechaNacimiento": "1990-04-18",
}
PENDIENTE = {
    "clienteId": CLIENTE_ID,
    "estado": "PENDIENTE",
    "verificadoEn": None,
    "proveedor": None,
    "referencia": None,
}
VERIFICADO = {
    "clienteId": CLIENTE_ID,
    "estado": "VERIFICADO",
    "verificadoEn": "2026-10-17T17:00:00Z",
    "proveedor": "KYC",
    "referencia": "kyc-ref-001",
}


def _b2(estado: int, codigo: str, detalles: dict[str, str] | None = None) -> httpx.Response:
    cuerpo = {"codigo": codigo, "mensaje": "m", "correlationId": "c", "detalles": detalles or {}}
    return httpx.Response(estado, json=cuerpo)


class ClientesFalso:
    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.respuesta: httpx.Response | Exception = httpx.Response(201, json=PENDIENTE)

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return self.respuesta


@pytest.fixture
def servicio(monkeypatch: pytest.MonkeyPatch) -> Iterator[ClientesFalso]:
    falso = ClientesFalso()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = ClientesCliente("http://clientes:8080", 5000, httpx.MockTransport(falso))
    app.dependency_overrides[ruta.clientes] = lambda: Clientes(cliente)
    yield falso
    app.dependency_overrides.clear()


def _verificar(encabezados: dict[str, str] | None = None) -> httpx.Response:
    return TestClient(app).post(
        f"/v1/clientes/{CLIENTE_ID}/identidad/verificaciones", headers=encabezados or ASESOR
    )


@pytest.mark.parametrize(
    "encabezados",
    [
        {"X-Authenticated-Issuer": BACKOFFICE, "X-Authenticated-Sub": "s"},
        {
            "X-Authenticated-Groups": "[operacion]",
            "X-Authenticated-Issuer": BACKOFFICE,
            "X-Authenticated-Sub": "s",
        },
        {
            "X-Authenticated-Groups": "[asesores]",
            "X-Authenticated-Issuer": "https://otro",
            "X-Authenticated-Sub": "s",
        },
    ],
    ids=["sin-grupo", "otro-grupo", "otro-emisor"],
)
def test_solo_asesores_del_backoffice(servicio: ClientesFalso, encabezados: dict[str, str]) -> None:
    for metodo, url in (
        ("post", "/v1/clientes"),
        ("get", f"/v1/clientes/{CLIENTE_ID}"),
        ("post", f"/v1/clientes/{CLIENTE_ID}/identidad/verificaciones"),
    ):
        respuesta = TestClient(app).request(metodo, url, json=ALTA, headers=encabezados)
        assert respuesta.status_code == 403
        assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
        assert set(respuesta.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert servicio.solicitudes == []


def test_sin_usuario_autenticado_es_401(servicio: ClientesFalso) -> None:
    sin_sub = {k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}
    respuesta = TestClient(app).post("/v1/clientes", json=ALTA, headers=sin_sub)
    assert respuesta.status_code == 401
    assert respuesta.json()["codigo"] == "ACTOR_REQUERIDO"
    assert servicio.solicitudes == []


def test_alta_reenvia_con_el_rol_asesor_y_responde_201(servicio: ClientesFalso) -> None:
    respuesta = TestClient(app).post("/v1/clientes", json=ALTA, headers=ASESOR)
    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json() == PENDIENTE
    (solicitud,) = servicio.solicitudes
    assert (solicitud.method, solicitud.url.path) == ("POST", "/v1/clientes")
    assert solicitud.headers["X-Actor-Id"] == "sub-asesora"
    assert solicitud.headers["X-Actor-Rol"] == "ASESOR"
    assert json.loads(solicitud.content) == ALTA


def test_alta_de_un_documento_existente_responde_200(servicio: ClientesFalso) -> None:
    servicio.respuesta = httpx.Response(200, json=PENDIENTE)
    respuesta = TestClient(app).post("/v1/clientes", json=ALTA, headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json() == PENDIENTE


@pytest.mark.parametrize(
    "cambios",
    [
        {"tipoDocumento": "cc"},
        {"numeroDocumento": "12"},
        {"nombreCompleto": ""},
        {"fechaNacimiento": "18/04/1990"},
    ],
)
def test_alta_invalida_es_422_sin_llamar_a_clientes(
    servicio: ClientesFalso, cambios: dict[str, str]
) -> None:
    respuesta = TestClient(app).post("/v1/clientes", json={**ALTA, **cambios}, headers=ASESOR)
    assert respuesta.status_code == 422
    assert servicio.solicitudes == []


def test_el_422_de_clientes_pasa_igual(servicio: ClientesFalso) -> None:
    servicio.respuesta = _b2(422, "SOLICITUD_INVALIDA")
    respuesta = TestClient(app).post("/v1/clientes", json=ALTA, headers=ASESOR)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")


def test_consultar_cliente(servicio: ClientesFalso) -> None:
    servicio.respuesta = httpx.Response(200, json=VERIFICADO)
    respuesta = TestClient(app).get(f"/v1/clientes/{CLIENTE_ID}", headers=ASESOR)
    assert respuesta.status_code == 200
    assert respuesta.json() == VERIFICADO
    assert servicio.solicitudes[0].url.path == f"/v1/clientes/{CLIENTE_ID}"


def test_verificar_aprobado_devuelve_fecha_y_referencia(servicio: ClientesFalso) -> None:
    """AC1."""
    servicio.respuesta = httpx.Response(200, json=VERIFICADO)
    respuesta = _verificar()
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == VERIFICADO
    (solicitud,) = servicio.solicitudes
    assert solicitud.url.path == f"/v1/clientes/{CLIENTE_ID}/identidad/verificacion-documental"


def test_verificar_rechazado_no_expone_el_motivo(servicio: ClientesFalso) -> None:
    """AC2: el portal solo sabe que la identidad no pudo verificarse."""
    rechazado = {**PENDIENTE, "estado": "RECHAZADO", "proveedor": "KYC", "referencia": "r-2"}
    servicio.respuesta = httpx.Response(200, json={**rechazado, "motivo": "DATOS_NO_COINCIDEN"})
    respuesta = _verificar()
    assert respuesta.json() == rechazado
    assert "DATOS_NO_COINCIDEN" not in respuesta.text


def test_falla_del_proveedor_conserva_el_estado_y_no_es_rechazo(servicio: ClientesFalso) -> None:
    """AC3: 503 con el estado pendiente, para ofrecer reintentar."""
    servicio.respuesta = _b2(
        503, "DEPENDENCIA_NO_DISPONIBLE", {"motivo": "ERROR", "estado": "PENDIENTE"}
    )
    respuesta = _verificar()
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"motivo": "ERROR", "estado": "PENDIENTE"}
    assert respuesta.json()["correlationId"] == "corr-bff-w30"


@pytest.mark.parametrize(
    ("estado", "codigo"), [(404, "CLIENTE_NO_ENCONTRADO"), (409, "CLIENTE_SIN_DOCUMENTO")]
)
def test_errores_de_negocio_pasan_con_su_estado(
    servicio: ClientesFalso, estado: int, codigo: str
) -> None:
    servicio.respuesta = _b2(estado, codigo)
    respuesta = _verificar()
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)


@pytest.mark.parametrize(
    "falla",
    [
        httpx.ReadTimeout("lento"),
        httpx.ConnectError("caído"),
        httpx.Response(500),
        httpx.Response(502, json={"detail": "x"}),
        httpx.Response(504, json={"codigo": "TIEMPO_AGOTADO"}),
    ],
    ids=["timeout", "conexion", "500", "502-sin-b2", "504"],
)
def test_falla_tecnica_es_servicio_no_disponible_sin_detalles_internos(
    servicio: ClientesFalso, falla: httpx.Response | Exception
) -> None:
    servicio.respuesta = falla
    respuesta = _verificar()
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert "clientes:8080" not in respuesta.text
