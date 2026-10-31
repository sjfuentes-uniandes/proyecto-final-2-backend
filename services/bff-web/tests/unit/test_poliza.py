"""UT-W17 (bff-web, 17.11): la póliza y la URL de su documento en el portal (HU-W17) con polizas
simulado.

Solo `asesores` consulta (los demás grupos, 403 sin llamar a polizas); actor, rol `ASESOR` y
correlación hacia polizas; los 4xx que polizas promete para cada ruta pasan tal cual y todo lo demás
es 503 SERVICIO_NO_DISPONIBLE sin detalles (falla cerrada). La URL prefirmada nunca va a los logs.
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import polizas as ruta
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.poliza import Polizas
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
COTIZACION_ID = "00000000-0000-4000-a000-000000000001"
POLIZA_ID = "00000000-0000-4000-9000-000000000001"
RUTA_POLIZA = f"/v1/cotizaciones/{COTIZACION_ID}/poliza"
RUTA_DOCUMENTO = f"/v1/polizas/{POLIZA_ID}/documento"
EMITIENDO: dict[str, Any] = {
    "polizaId": POLIZA_ID,
    "estado": "EMITIENDO",
    "numero": "100-2026-000001",
    "producto": {"id": "vida-hipotecario", "version": "2026.09.1"},
    "ramo": {"codigo": "100", "nombre": "Vida hipotecario"},
    "coberturas": [
        {"codigo": "MUERTE", "nombre": "Muerte por cualquier causa", "obligatoria": True},
        {
            "codigo": "INCAPACIDAD_TOTAL_PERMANENTE",
            "nombre": "Incapacidad total y permanente",
            "obligatoria": True,
        },
    ],
    "vigencia": {"desde": "2026-10-25", "hasta": "2027-10-24"},
    "sumaAsegurada": "180000000",
    "prima": "64638",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "aceptadaEn": "2026-10-24T15:00:00.123456Z",
    "recaudadaEn": "2026-10-24T15:00:01.250000Z",
    "emitidaEn": None,
    "documento": None,
    "firma": None,
}
HUELLA = "sha256:" + "ab" * 32
ACTIVA: dict[str, Any] = {
    **EMITIENDO,
    "estado": "ACTIVA",
    "emitidaEn": "2026-10-24T15:00:03.500000Z",
    "documento": {"huella": HUELLA, "versionId": "v-sintetica-0001"},
    "firma": {"firmaId": "FIR-SINT-C01", "selloTiempo": "2026-01-01T00:00:00Z"},
}
FIRMA_URL = "X-Amz-Signature=" + "f" * 64
URL = (
    f"https://polizas-documentos.s3.amazonaws.com/polizas/{POLIZA_ID}/100-2026-000001.pdf"
    f"?versionId=v-sintetica-0001&X-Amz-Algorithm=AWS4-HMAC-SHA256&{FIRMA_URL}"
)
ENLACE = {"url": URL, "venceEn": "2026-10-24T15:05:03.500000Z"}


def _usuario(grupos: str, sub: str = "sub-asesor") -> dict[str, str]:
    return {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Authenticated-Sub": sub,
        "X-Correlation-Id": "corr-bff-w17",
    }


ASESOR = _usuario("[asesores]")
SUSCRIPTOR = _usuario("[suscriptores]", "sub-suscriptora")
OPERACION = _usuario("[operacion]")


class Simulado:
    """polizas: la póliza por la ruta 1 y el enlace por la ruta 2, salvo que se cambie `responder`."""

    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = self._por_ruta

    @staticmethod
    def _por_ruta(pedido: httpx.Request) -> httpx.Response:
        if pedido.url.path.endswith("/documento"):
            return httpx.Response(200, json=ENLACE)
        return httpx.Response(200, json=EMITIENDO)

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)


@pytest.fixture
def polizas(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulado]:
    simulado = Simulado()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = PolizasCliente("http://polizas:8080", 1500, httpx.MockTransport(simulado))
    app.dependency_overrides[ruta.polizas] = lambda: Polizas(cliente)
    yield simulado
    app.dependency_overrides.clear()


def _get(ruta_: str, encabezados: dict[str, str] = ASESOR) -> httpx.Response:
    return TestClient(app).get(ruta_, headers=encabezados)


RUTAS = pytest.mark.parametrize("ruta_", [RUTA_POLIZA, RUTA_DOCUMENTO], ids=["poliza", "documento"])


# --- Lo que el portal recibe ------------------------------------------------------------------


@pytest.mark.parametrize("poliza", [EMITIENDO, ACTIVA], ids=lambda p: p["estado"])
def test_el_asesor_consulta_la_poliza(polizas: Simulado, poliza: dict[str, Any]) -> None:
    polizas.responder = lambda _: httpx.Response(200, json=poliza)
    respuesta = _get(RUTA_POLIZA)
    assert (respuesta.status_code, respuesta.json()) == (200, poliza)


def test_el_asesor_obtiene_la_url_del_documento(polizas: Simulado) -> None:
    respuesta = _get(RUTA_DOCUMENTO)
    assert (respuesta.status_code, respuesta.json()) == (200, ENLACE)


def test_los_campos_internos_de_polizas_no_llegan_al_portal(polizas: Simulado) -> None:
    """Si polizas agregara campos (contrato aditivo), el portal recibe solo los del DTO."""
    polizas.responder = lambda pedido: httpx.Response(
        200,
        json={**ENLACE, "clave": "polizas/x.pdf"}
        if pedido.url.path.endswith("/documento")
        else {**ACTIVA, "clienteId": "00000000-0000-4000-8000-000000000001"},
    )
    assert _get(RUTA_POLIZA).json() == ACTIVA
    assert _get(RUTA_DOCUMENTO).json() == ENLACE


# --- El reenvío -------------------------------------------------------------------------------


@RUTAS
def test_reenvia_a_polizas_con_el_actor_el_rol_y_la_correlacion(
    polizas: Simulado, ruta_: str
) -> None:
    _get(ruta_)
    [enviado] = polizas.pedidos
    assert (str(enviado.url), enviado.method) == (f"http://polizas:8080{ruta_}", "GET")
    assert enviado.headers["X-Actor-Id"] == "sub-asesor"
    assert enviado.headers["X-Actor-Rol"] == "ASESOR"
    assert enviado.headers["X-Correlation-Id"] == "corr-bff-w17"
    assert enviado.content == b""


@RUTAS
@pytest.mark.parametrize(
    ("encabezados", "estado", "codigo"),
    [
        (SUSCRIPTOR, 403, "ROL_NO_AUTORIZADO"),
        (OPERACION, 403, "ROL_NO_AUTORIZADO"),
        ({**ASESOR, "X-Authenticated-Issuer": "https://otro"}, 403, "ROL_NO_AUTORIZADO"),
        ({k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}, 401, "ACTOR_REQUERIDO"),
        ({**ASESOR, "X-Authenticated-Sub": ""}, 401, "ACTOR_REQUERIDO"),
    ],
    ids=["suscriptores", "operacion", "otro-emisor", "sin-sub", "sub-vacio"],
)
def test_solo_asesores_identificados(
    polizas: Simulado, ruta_: str, encabezados: dict[str, str], estado: int, codigo: str
) -> None:
    respuesta = _get(ruta_, encabezados)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    assert polizas.pedidos == []


@pytest.mark.parametrize(
    "ruta_",
    ["/v1/cotizaciones/no-es-uuid/poliza", "/v1/polizas/no-es-uuid/documento"],
    ids=["poliza", "documento"],
)
def test_un_id_que_no_es_uuid_no_llama_a_polizas(polizas: Simulado, ruta_: str) -> None:
    respuesta = _get(ruta_)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert polizas.pedidos == []


# --- Errores: lista blanca por ruta y falla cerrada -------------------------------------------

COMUNES = [
    (401, "ACTOR_REQUERIDO", {}),
    (403, "ROL_NO_AUTORIZADO", {}),
    (422, "SOLICITUD_INVALIDA", {"campos": ["polizaId"]}),
]


@pytest.mark.parametrize(
    ("ruta_", "estado", "codigo", "detalles"),
    [
        *[(RUTA_POLIZA, *c) for c in COMUNES],
        (RUTA_POLIZA, 404, "ACEPTACION_NO_ENCONTRADA", {}),
        (RUTA_POLIZA, 404, "POLIZA_NO_ENCONTRADA", {}),
        *[(RUTA_DOCUMENTO, *c) for c in COMUNES],
        (RUTA_DOCUMENTO, 404, "POLIZA_NO_ENCONTRADA", {}),
        (RUTA_DOCUMENTO, 409, "POLIZA_NO_ACTIVA", {}),
    ],
    ids=lambda v: v if isinstance(v, str) and not v.startswith("/") else None,
)
def test_los_errores_de_la_lista_blanca_pasan_tal_cual(
    polizas: Simulado, ruta_: str, estado: int, codigo: str, detalles: dict[str, Any]
) -> None:
    polizas.responder = lambda _: httpx.Response(
        estado, json={"codigo": codigo, "mensaje": "mensaje de polizas", "detalles": detalles}
    )
    respuesta = _get(ruta_)
    assert respuesta.status_code == estado
    assert respuesta.json() == {
        "codigo": codigo,
        "mensaje": "mensaje de polizas",
        "correlationId": "corr-bff-w17",
        "detalles": detalles,
    }


def _caido(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("polizas caído")


def _lento(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("polizas no respondió a tiempo")


def _json(estado: int, cuerpo: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(estado, json=cuerpo)


SIN_FIRMA = {k: v for k, v in ACTIVA.items() if k != "firma"}
CAUSAS_COMUNES = {
    "503-dependencia": _json(
        503,
        {
            "codigo": "DEPENDENCIA_NO_DISPONIBLE",
            "mensaje": "m",
            "detalles": {"dependencia": "documentos"},
        },
    ),
    "500": _json(500, {"codigo": "ERROR_INTERNO", "detalles": {"x": 1}}),
    "5xx-con-codigo-de-la-lista": _json(502, {"codigo": "POLIZA_NO_ENCONTRADA"}),
    "4xx-fuera-de-la-lista": _json(409, {"codigo": "OFERTA_VENCIDA", "detalles": {"a": 1}}),
    "404-de-otro-codigo": _json(404, {"codigo": "DECISION_NO_ENCONTRADA"}),
    "404-no-json": lambda _: httpx.Response(404, text="<html>no encontrado</html>"),
    "no-json": lambda _: httpx.Response(200, text="<html>"),
    "timeout": _lento,
    "caido": _caido,
}
CAUSAS_POLIZA = {
    **CAUSAS_COMUNES,
    # Un código que polizas promete en la otra ruta, no en esta.
    "409-de-la-otra-ruta": _json(409, {"codigo": "POLIZA_NO_ACTIVA"}),
    "no-objeto": _json(200, [ACTIVA]),
    "estado-desconocido": _json(200, {**ACTIVA, "estado": "ANULADA"}),
    "prima-float": _json(200, {**ACTIVA, "prima": 64638.0}),
    "numero-sin-formato": _json(200, {**ACTIVA, "numero": "2026-1"}),
    "huella-sin-formato": _json(200, {**ACTIVA, "documento": {"huella": "x", "versionId": "v"}}),
    "ramo-sin-formato": _json(200, {**ACTIVA, "ramo": {"codigo": "1", "nombre": "Vida"}}),
    "vigencia-sin-fecha": _json(200, {**ACTIVA, "vigencia": {"desde": "mañana", "hasta": None}}),
    "falta-un-campo": _json(200, SIN_FIRMA),
}
CAUSAS_DOCUMENTO = {
    **CAUSAS_COMUNES,
    "404-de-la-otra-ruta": _json(404, {"codigo": "ACEPTACION_NO_ENCONTRADA"}),
    "no-objeto": _json(200, [ENLACE]),
    "sin-url": _json(200, {"venceEn": ENLACE["venceEn"]}),
    "url-no-texto": _json(200, {**ENLACE, "url": 1}),
    "vence-sin-fecha": _json(200, {**ENLACE, "venceEn": "pronto"}),
}


def _es_503_sin_detalles(respuesta: httpx.Response) -> None:
    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert (cuerpo["codigo"], cuerpo["detalles"], cuerpo["correlationId"]) == (
        "SERVICIO_NO_DISPONIBLE",
        {},
        "corr-bff-w17",
    )


@pytest.mark.parametrize("causa", list(CAUSAS_POLIZA))
def test_la_poliza_por_todo_lo_demas_es_503_sin_detalles(polizas: Simulado, causa: str) -> None:
    polizas.responder = CAUSAS_POLIZA[causa]
    _es_503_sin_detalles(_get(RUTA_POLIZA))


@pytest.mark.parametrize("causa", list(CAUSAS_DOCUMENTO))
def test_el_documento_por_todo_lo_demas_es_503_sin_detalles(polizas: Simulado, causa: str) -> None:
    polizas.responder = CAUSAS_DOCUMENTO[causa]
    _es_503_sin_detalles(_get(RUTA_DOCUMENTO))


# --- Medición y logs --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ruta_", "operacion_", "respuesta", "resultado"),
    [
        (RUTA_POLIZA, "poliza.consultar", _json(200, ACTIVA), "ok"),
        (
            RUTA_POLIZA,
            "poliza.consultar",
            _json(404, {"codigo": "POLIZA_NO_ENCONTRADA"}),
            "rechazado",
        ),
        (RUTA_POLIZA, "poliza.consultar", _json(500, {}), "error"),
        (RUTA_POLIZA, "poliza.consultar", _lento, "error"),
        (RUTA_DOCUMENTO, "poliza.documento", _json(200, ENLACE), "ok"),
        (
            RUTA_DOCUMENTO,
            "poliza.documento",
            _json(409, {"codigo": "POLIZA_NO_ACTIVA"}),
            "rechazado",
        ),
        (RUTA_DOCUMENTO, "poliza.documento", _caido, "error"),
    ],
)
def test_cada_ruta_mide_su_operacion(
    polizas: Simulado,
    caplog: pytest.LogCaptureFixture,
    ruta_: str,
    operacion_: str,
    respuesta: Callable[[httpx.Request], httpx.Response],
    resultado: str,
) -> None:
    polizas.responder = respuesta
    with caplog.at_level(logging.INFO):
        _get(ruta_)
    medidas = [
        getattr(r, "result", None)
        for r in caplog.records
        if getattr(r, "operation", None) == operacion_
    ]
    assert medidas == [resultado]


def test_la_url_y_los_datos_de_la_poliza_no_van_a_los_logs(
    polizas: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert _get(RUTA_DOCUMENTO).status_code == 200
        # Fuera del DTO (con la URL en el cuerpo): 503, y la URL tampoco va al log.
        polizas.responder = _json(200, {**ENLACE, "venceEn": "pronto"})
        assert _get(RUTA_DOCUMENTO).status_code == 503
        polizas.responder = _json(500, ENLACE)
        assert _get(RUTA_DOCUMENTO).status_code == 503
        polizas.responder = _json(200, ACTIVA)
        assert _get(RUTA_POLIZA).status_code == 200
        polizas.responder = _json(200, {**ACTIVA, "prima": 64638.0})
        assert _get(RUTA_POLIZA).status_code == 503
    texto = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert caplog.records, "sin registros: la prueba no observaría nada"
    for centinela in (
        URL,
        FIRMA_URL,
        "polizas-documentos",
        HUELLA,
        "100-2026-000001",
        "sub-asesor",
    ):
        assert centinela not in texto, centinela


def test_el_servicio_por_omision_usa_la_configuracion() -> None:
    ruta.polizas.cache_clear()
    servicio = ruta.polizas()
    assert servicio.polizas.url == settings.polizas_url.rstrip("/")
    assert servicio.polizas.timeout == settings.polizas_timeout_ms / 1000
    ruta.polizas.cache_clear()
