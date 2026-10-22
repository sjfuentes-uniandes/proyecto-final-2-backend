"""HU-W03: rutas /v1/cotizaciones y /v1/productos/{id}/condiciones del portal con cotizacion
simulada: solo asesores, el actor es el asesor, los B2 de cotizacion pasan y el resto es 503."""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import cotizaciones as ruta
from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.application.cotizaciones import Cotizaciones
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
    "X-Correlation-Id": "corr-bff-w03",
}
CLAVE = "00000000-0000-4000-c000-000000000001"
COTIZACION_ID = "00000000-0000-4000-d000-000000000001"
SOLICITUD = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
    "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
}
COTIZACION = {
    "id": COTIZACION_ID,
    "clienteId": SOLICITUD["clienteId"],
    "socioId": None,
    "productoId": "vida-hipotecario",
    "versionProducto": "2026.09.1",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "versionReglas": "2026.09.1",
    "perfilId": None,
    "estado": "CALCULADA",
    "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
    "entradas": {
        "sumaAsegurada": "180000000",
        "plazoCreditoMeses": 180,
        "edad": 36,
        "nivelRiesgo": None,
        "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
    },
    "desglose": {
        "primaBase": "75600",
        "factores": [{"codigo": "NIVEL_RIESGO", "valor": "SIN_PERFIL", "factor": "1.15"}],
        "primaAjustada": "82593",
        "recargos": [{"cobertura": "ENFERMEDADES_GRAVES", "tasa": "0.18", "valor": "14867"}],
        "subtotal": "97460",
        "ajusteLimite": "0",
        "primaTotal": "97460",
    },
    "primaTotal": "97460",
    "huellaCalculo": "sha256:abc",
    "calculadoEn": "2026-10-15T15:00:00Z",
    "duracionMs": 40,
    "presupuestoMs": 250,
    "pasos": [{"codigo": "CATALOGO_Y_REGLAS", "duracionMs": 11, "origen": "CACHE"}],
}
CONDICIONES = {
    "productoId": "vida-hipotecario",
    "nombre": "Vida deudor hipotecario",
    "ramo": "VIDA_HIPOTECARIO",
    "pais": "CO",
    "moneda": "COP",
    "versionProducto": "2026.09.1",
    "versionReglas": "2026.09.1",
    "periodicidad": "MENSUAL",
    "vigenteHasta": None,
    "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
    "limites": {
        "sumaAsegurada": {"minimo": "10000000", "maximo": "1000000000"},
        "plazoCreditoMeses": {"minimo": "12", "maximo": "360"},
        "edad": {"minimo": "18", "maximo": "70"},
    },
}


class CotizacionFalsa:
    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.respuesta: httpx.Response | Exception = httpx.Response(201, json=COTIZACION)

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        if solicitud.url.path.endswith("/condiciones"):
            return httpx.Response(200, json=CONDICIONES)
        if solicitud.method == "GET":
            return httpx.Response(200, json=COTIZACION)
        return self.respuesta


@pytest.fixture
def cotizacion(monkeypatch: pytest.MonkeyPatch) -> Iterator[CotizacionFalsa]:
    falsa = CotizacionFalsa()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = CotizacionCliente("http://cotizacion:8080", 1500, httpx.MockTransport(falsa))
    app.dependency_overrides[ruta.cotizaciones] = lambda: Cotizaciones(cliente)
    yield falsa
    app.dependency_overrides.clear()


def _cotizar(encabezados: dict[str, str] | None = None) -> httpx.Response:
    return TestClient(app).post(
        "/v1/cotizaciones",
        json=SOLICITUD,
        headers={**ASESOR, "Idempotency-Key": CLAVE} if encabezados is None else encabezados,
    )


def test_reenvia_como_asesor_con_la_clave(cotizacion: CotizacionFalsa) -> None:
    respuesta = _cotizar()
    assert respuesta.status_code == 201
    assert respuesta.json() == COTIZACION
    (enviada,) = cotizacion.solicitudes
    assert enviada.url.path == "/v1/cotizaciones"
    assert enviada.headers["X-Actor-Id"] == "sub-asesora"
    assert enviada.headers["X-Actor-Rol"] == "ASESOR"
    assert enviada.headers["Idempotency-Key"] == CLAVE
    assert enviada.headers["X-Correlation-Id"] == "corr-bff-w03"
    cuerpo = enviada.read()
    assert b'"perfilId"' not in cuerpo and b'"socioId"' not in cuerpo


def test_misma_clave_devuelve_200(cotizacion: CotizacionFalsa) -> None:
    cotizacion.respuesta = httpx.Response(200, json=COTIZACION)
    assert _cotizar().status_code == 200


def test_lectura_y_condiciones(cotizacion: CotizacionFalsa) -> None:
    cliente = TestClient(app)
    leida = cliente.get(f"/v1/cotizaciones/{COTIZACION_ID}", headers=ASESOR)
    condiciones = cliente.get("/v1/productos/vida-hipotecario/condiciones", headers=ASESOR)
    assert leida.json()["primaTotal"] == "97460"
    assert condiciones.json() == CONDICIONES
    assert [s.url.path for s in cotizacion.solicitudes] == [
        f"/v1/cotizaciones/{COTIZACION_ID}",
        "/v1/productos/vida-hipotecario/condiciones",
    ]


@pytest.mark.parametrize(
    ("respuesta", "estado", "codigo"),
    [
        (
            httpx.Response(422, json={"codigo": "PRODUCTO_FUERA_DE_VIGENCIA", "mensaje": "m"}),
            422,
            "PRODUCTO_FUERA_DE_VIGENCIA",
        ),
        (
            httpx.Response(
                422,
                json={
                    "codigo": "DATOS_FUERA_DE_RANGO",
                    "mensaje": "m",
                    "detalles": {"campos": ["datosAsegurado.sumaAsegurada"]},
                },
            ),
            422,
            "DATOS_FUERA_DE_RANGO",
        ),
        (
            httpx.Response(503, json={"codigo": "DEPENDENCIA_NO_DISPONIBLE", "mensaje": "m"}),
            503,
            "DEPENDENCIA_NO_DISPONIBLE",
        ),
        (httpx.Response(500, text="falla"), 503, "SERVICIO_NO_DISPONIBLE"),
        (httpx.ConnectError("sin red"), 503, "SERVICIO_NO_DISPONIBLE"),
    ],
)
def test_errores(cotizacion: CotizacionFalsa, respuesta: Any, estado: int, codigo: str) -> None:
    cotizacion.respuesta = respuesta
    resultado = _cotizar()
    assert (resultado.status_code, resultado.json()["codigo"]) == (estado, codigo)
    if codigo == "SERVICIO_NO_DISPONIBLE":
        assert resultado.json()["mensaje"] == "El servicio de cotización no está disponible"
        assert "cotizacion:8080" not in resultado.text
    if codigo == "DATOS_FUERA_DE_RANGO":
        assert resultado.json()["detalles"] == {"campos": ["datosAsegurado.sumaAsegurada"]}


@pytest.mark.parametrize(
    "encabezados",
    [
        {"X-Authenticated-Issuer": BACKOFFICE, "X-Authenticated-Sub": "s"},
        {
            "X-Authenticated-Groups": "[operacion]",
            "X-Authenticated-Issuer": BACKOFFICE,
            "X-Authenticated-Sub": "s",
        },
    ],
    ids=["sin-grupo", "otro-grupo"],
)
def test_solo_asesores(cotizacion: CotizacionFalsa, encabezados: dict[str, str]) -> None:
    cliente = TestClient(app)
    for metodo, url in (
        ("post", "/v1/cotizaciones"),
        ("get", f"/v1/cotizaciones/{COTIZACION_ID}"),
        ("get", "/v1/productos/vida-hipotecario/condiciones"),
    ):
        respuesta = cliente.request(
            metodo, url, json=SOLICITUD, headers={**encabezados, "Idempotency-Key": CLAVE}
        )
        assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    assert cotizacion.solicitudes == []


def test_sin_sub_es_actor_requerido(cotizacion: CotizacionFalsa) -> None:
    sin_sub = {k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}
    respuesta = _cotizar({**sin_sub, "Idempotency-Key": CLAVE})
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (401, "ACTOR_REQUERIDO")


def test_dependencia_real_por_omision() -> None:
    assert isinstance(ruta.cotizaciones(), Cotizaciones)
