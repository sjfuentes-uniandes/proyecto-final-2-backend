"""HU-W11: rutas /v1/perfiles del portal (W11-P15) con cotizacion simulada."""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import perfilamiento as ruta
from bff_web.adapters.outbound.cotizacion import CotizacionCliente
from bff_web.application.perfilamiento import Perfilamiento
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient
from solventa_common.logging import FormateadorJson

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
ASESOR = {
    "X-Authenticated-Groups": "[asesores]",
    "X-Authenticated-Issuer": BACKOFFICE,
    "X-Authenticated-Sub": "sub-asesora",
    "X-Correlation-Id": "corr-bff-w11",
}
CLAVE = "00000000-0000-4000-b000-0000000000c1"
PERFIL_ID = "00000000-0000-4000-a000-000000000001"
C01 = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosDeclarados": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
}
PERFIL = {
    "id": PERFIL_ID,
    "clienteId": C01["clienteId"],
    "productoId": "vida-hipotecario",
    "estado": "CALCULADO",
    "nivelRiesgo": "BAJO",
    "puntaje": 75.9,
    "confianza": 0.92,
    "factores": [
        {
            "codigo": "PUNTUALIDAD_PAGOS",
            "fuente": "FA_HISTORIAL_PAGOS_12M",
            "peso": 0.3,
            "estado": "EVALUADO",
            "contribucion": 0.9,
            "direccion": "REDUCE_RIESGO",
            "motivo": None,
        },
    ],
    "ajustes": [],
    "datosRequeridos": [],
    "accionSugerida": None,
    "consentimiento": {
        "decision": "PERMITIDO",
        "causa": None,
        "vigenteHasta": "2027-10-01T00:00:00Z",
    },
    "fuentes": [
        {
            "fuente": "FA_PRODUCTOS_VIGENTES",
            "tipo": "FINANZAS_ABIERTAS",
            "estado": "CONSULTADA",
            "versionFuente": "3.1",
            "confianza": 0.95,
            "consultadaEn": "2026-10-15T15:00:00.100000Z",
            "duracionMs": 52,
        },
    ],
    "fuentesConsultadas": 4,
    "fuentesDeclaradas": 4,
    "versionModelo": "2026.10.1",
    "calculadoEn": "2026-10-15T15:00:00.212000Z",
    "duracionMs": 212,
    "presupuestoMs": 400,
    "perfilAnteriorId": None,
}
RESUMEN = {
    k: PERFIL[k]
    for k in (
        "id",
        "estado",
        "nivelRiesgo",
        "confianza",
        "versionModelo",
        "calculadoEn",
        "perfilAnteriorId",
    )
}


class CotizacionFalsa:
    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.respuesta: httpx.Response | Exception = httpx.Response(201, json=PERFIL)
        # GET /v1/perfiles/{id} con respuesta propia; sin entrada, PERFIL.
        self.por_id: dict[str, httpx.Response] = {}

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        if solicitud.method == "GET" and solicitud.url.path == "/v1/perfiles":
            return httpx.Response(200, json={"items": [RESUMEN]})
        if solicitud.method == "GET":
            return self.por_id.get(
                solicitud.url.path.rsplit("/", 1)[-1], httpx.Response(200, json=PERFIL)
            )
        return self.respuesta


@pytest.fixture
def cotizacion(monkeypatch: pytest.MonkeyPatch) -> Iterator[CotizacionFalsa]:
    falsa = CotizacionFalsa()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = CotizacionCliente("http://cotizacion:8080", 1500, httpx.MockTransport(falsa))
    app.dependency_overrides[ruta.perfilamiento] = lambda: Perfilamiento(cliente)
    yield falsa
    app.dependency_overrides.clear()


def _crear(
    encabezados: dict[str, str] | None = None, cuerpo: dict[str, Any] | None = None
) -> httpx.Response:
    return TestClient(app).post(
        "/v1/perfiles",
        json=C01 if cuerpo is None else cuerpo,
        headers={**ASESOR, "Idempotency-Key": CLAVE} if encabezados is None else encabezados,
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
def test_solo_asesores_del_backoffice(
    cotizacion: CotizacionFalsa, encabezados: dict[str, str]
) -> None:
    for metodo, url in (
        ("post", "/v1/perfiles"),
        ("get", f"/v1/perfiles/{PERFIL_ID}"),
        ("get", f"/v1/perfiles?clienteId={C01['clienteId']}"),
        ("post", f"/v1/perfiles/{PERFIL_ID}/recalculos"),
        ("get", f"/v1/perfiles/{PERFIL_ID}/comparacion"),
    ):
        respuesta = TestClient(app).request(
            metodo, url, json=C01, headers={**encabezados, "Idempotency-Key": CLAVE}
        )
        assert respuesta.status_code == 403
        assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
        assert set(respuesta.json()) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert cotizacion.solicitudes == []


def test_crear_reenvia_actor_rol_clave_y_cuerpo(cotizacion: CotizacionFalsa) -> None:
    respuesta = _crear()
    assert respuesta.status_code == 201
    assert respuesta.json() == PERFIL
    (enviada,) = cotizacion.solicitudes
    assert (enviada.method, enviada.url.path) == ("POST", "/v1/perfiles")
    assert enviada.headers["X-Actor-Id"] == "sub-asesora"
    assert enviada.headers["X-Actor-Rol"] == "ASESOR"
    assert enviada.headers["Idempotency-Key"] == CLAVE
    assert enviada.headers["X-Correlation-Id"] == "corr-bff-w11"
    assert "x-authenticated-groups" not in enviada.headers
    assert json.loads(enviada.content) == C01


def test_misma_clave_responde_200(cotizacion: CotizacionFalsa) -> None:
    cotizacion.respuesta = httpx.Response(200, json=PERFIL)
    assert _crear().status_code == 200


def test_sin_usuario_autenticado_responde_401(cotizacion: CotizacionFalsa) -> None:
    encabezados = {k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"} | {
        "Idempotency-Key": CLAVE
    }
    respuesta = _crear(encabezados)
    assert respuesta.status_code == 401
    assert respuesta.json()["codigo"] == "ACTOR_REQUERIDO"
    assert cotizacion.solicitudes == []


@pytest.mark.parametrize(
    ("cuerpo", "campo"),
    [
        ({**C01, "productoId": "Vida"}, "productoId"),
        (
            {**C01, "datosDeclarados": {**C01["datosDeclarados"], "plazoCreditoMeses": 400}},
            "datosDeclarados.plazoCreditoMeses",
        ),
        ({k: v for k, v in C01.items() if k != "clienteId"}, "clienteId"),
    ],
)
def test_cuerpo_invalido_no_llega_a_cotizacion(
    cotizacion: CotizacionFalsa, cuerpo: dict[str, Any], campo: str
) -> None:
    respuesta = _crear(cuerpo=cuerpo)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert respuesta.json()["detalles"]["campos"] == [campo]
    assert cotizacion.solicitudes == []


@pytest.mark.parametrize("clave", [None, "no-es-uuid"])
def test_idempotency_key_obligatoria(cotizacion: CotizacionFalsa, clave: str | None) -> None:
    encabezados = dict(ASESOR) | ({"Idempotency-Key": clave} if clave else {})
    respuesta = _crear(encabezados)
    assert respuesta.status_code == 422
    assert respuesta.json()["detalles"]["campos"] == ["Idempotency-Key"]


@pytest.mark.parametrize(
    ("estado", "codigo", "detalles"),
    [
        (403, "IDENTIDAD_NO_VERIFICADA", {}),
        (404, "CLIENTE_NO_ENCONTRADO", {}),
        (409, "IDENTIDAD_PENDIENTE", {}),
        (422, "IDEMPOTENCIA_CONFLICTO", {}),
        (422, "PRODUCTO_NO_DISPONIBLE", {}),
        (422, "SOLICITUD_INVALIDA", {"campos": ["datosDeclarados.fechaNacimiento"]}),
        (503, "DEPENDENCIA_NO_DISPONIBLE", {"dependencia": "catalogo.modelo"}),
    ],
)
def test_errores_b2_de_cotizacion_pasan_sin_cambios(
    cotizacion: CotizacionFalsa, estado: int, codigo: str, detalles: dict[str, Any]
) -> None:
    cotizacion.respuesta = httpx.Response(
        estado,
        json={
            "codigo": codigo,
            "mensaje": "Mensaje de cotizacion",
            "correlationId": "otro",
            "detalles": detalles,
        },
    )
    respuesta = _crear()
    assert respuesta.status_code == estado
    assert respuesta.json() == {
        "codigo": codigo,
        "mensaje": "Mensaje de cotizacion",
        "correlationId": "corr-bff-w11",
        "detalles": detalles,
    }


@pytest.mark.parametrize(
    "falla",
    [
        httpx.Response(
            500, json={"codigo": "ERROR_INTERNO", "mensaje": "Error interno", "detalles": {}}
        ),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(504, json={"codigo": "TIEMPO_AGOTADO", "mensaje": "x", "detalles": {}}),
        httpx.ReadTimeout("lento"),
        httpx.ConnectError("sin conexión con http://cotizacion:8080"),
    ],
    ids=["500-b2", "502", "503-sin-b2", "504", "timeout", "conexion"],
)
def test_cotizacion_caida_o_lenta_responde_503(cotizacion: CotizacionFalsa, falla: Any) -> None:
    cotizacion.respuesta = falla
    respuesta = _crear()
    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {}
    for interno in ("cotizacion:8080", "http://", "cotizacion"):
        assert interno not in respuesta.text


def test_consultar_y_listar(cotizacion: CotizacionFalsa) -> None:
    cliente = TestClient(app)
    perfil = cliente.get(f"/v1/perfiles/{PERFIL_ID}", headers=ASESOR)
    assert (perfil.status_code, perfil.json()) == (200, PERFIL)
    lista = cliente.get(
        "/v1/perfiles", params={"clienteId": C01["clienteId"], "limite": 5}, headers=ASESOR
    )
    assert lista.json() == {"items": [RESUMEN]}
    detalle, listado = cotizacion.solicitudes
    assert detalle.url.path == f"/v1/perfiles/{PERFIL_ID}"
    assert dict(listado.url.params) == {"clienteId": C01["clienteId"], "limite": "5"}
    assert {s.headers["X-Actor-Rol"] for s in cotizacion.solicitudes} == {"ASESOR"}


def test_perfil_inexistente(cotizacion: CotizacionFalsa) -> None:
    cuerpo = {
        "codigo": "PERFIL_NO_ENCONTRADO",
        "mensaje": "El perfil no existe",
        "correlationId": None,
        "detalles": {},
    }
    cliente = CotizacionCliente(
        "http://cotizacion:8080",
        1500,
        httpx.MockTransport(lambda _: httpx.Response(404, json=cuerpo)),
    )
    app.dependency_overrides[ruta.perfilamiento] = lambda: Perfilamiento(cliente)
    respuesta = TestClient(app).get(f"/v1/perfiles/{PERFIL_ID}", headers=ASESOR)
    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "PERFIL_NO_ENCONTRADO"


@pytest.mark.parametrize(
    "params", [{"clienteId": "x"}, {}, {"clienteId": C01["clienteId"], "limite": 51}]
)
def test_listado_invalido(cotizacion: CotizacionFalsa, params: dict[str, Any]) -> None:
    respuesta = TestClient(app).get("/v1/perfiles", params=params, headers=ASESOR)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"


def test_las_rutas_de_hu_w27_conservan_su_formato() -> None:
    respuesta = TestClient(app).get("/operacion/trazas", params={"correlation_id": "x"})
    assert respuesta.status_code == 403
    assert set(respuesta.json()) == {"detail"}


def test_cors_permite_idempotency_key() -> None:
    respuesta = TestClient(app).options(
        "/v1/perfiles",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key,content-type",
        },
    )
    assert respuesta.status_code == 200
    assert "idempotency-key" in respuesta.headers["access-control-allow-headers"].lower()


def test_valores_declarados_ausentes_de_los_logs(cotizacion: CotizacionFalsa) -> None:
    raiz = logging.getLogger()
    anteriores, nivel = list(raiz.handlers), raiz.level
    salida = io.StringIO()
    manejador = logging.StreamHandler(salida)
    manejador.setFormatter(FormateadorJson("bff-web", "pruebas"))
    raiz.handlers, raiz.level = [manejador], logging.INFO
    try:
        _crear()
        cotizacion.respuesta = httpx.ConnectError("sin conexión")
        _crear()
    finally:
        raiz.handlers, raiz.level = anteriores, nivel
    for valor in ("180000000", "1990-04-18", "sub-asesora"):
        assert valor not in salida.getvalue()


def test_el_cliente_real_se_construye_con_la_configuracion() -> None:
    ruta.perfilamiento.cache_clear()
    servicio = ruta.perfilamiento()
    assert servicio.cotizacion.url == settings.cotizacion_url
    assert servicio.cotizacion.timeout == 1.5
    assert ruta.perfilamiento() is servicio


# --- HU-W12: recálculo y comparación (W12-P07) -------------------------------------------

NUEVO_ID = "00000000-0000-4000-a000-000000000002"
DEGRADADO = {**PERFIL, "estado": "DEGRADADO", "accionSugerida": "RECALCULAR"}
RECALCULADO = {**PERFIL, "id": NUEVO_ID, "perfilAnteriorId": PERFIL_ID}


def _recalcular(encabezados: dict[str, str] | None = None) -> httpx.Response:
    return TestClient(app).post(
        f"/v1/perfiles/{PERFIL_ID}/recalculos",
        headers={**ASESOR, "Idempotency-Key": CLAVE} if encabezados is None else encabezados,
    )


def test_recalcular_reenvia_actor_rol_y_clave(cotizacion: CotizacionFalsa) -> None:
    cotizacion.respuesta = httpx.Response(201, json=RECALCULADO)
    respuesta = _recalcular()
    assert respuesta.status_code == 201
    assert respuesta.json() == RECALCULADO
    (solicitud,) = cotizacion.solicitudes
    assert (solicitud.method, str(solicitud.url)) == (
        "POST",
        f"http://cotizacion:8080/v1/perfiles/{PERFIL_ID}/recalculos",
    )
    assert solicitud.headers["Idempotency-Key"] == CLAVE
    assert solicitud.headers["X-Actor-Id"] == "sub-asesora"
    assert solicitud.headers["X-Actor-Rol"] == "ASESOR"
    assert solicitud.headers["X-Correlation-Id"] == "corr-bff-w11"
    assert solicitud.content == b""


def test_recalcular_con_la_misma_clave_responde_200(cotizacion: CotizacionFalsa) -> None:
    cotizacion.respuesta = httpx.Response(200, json=RECALCULADO)
    assert _recalcular().status_code == 200


@pytest.mark.parametrize("clave", [None, "no-es-uuid"])
def test_recalcular_exige_idempotency_key(cotizacion: CotizacionFalsa, clave: str | None) -> None:
    encabezados = {**ASESOR} if clave is None else {**ASESOR, "Idempotency-Key": clave}
    respuesta = _recalcular(encabezados)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SOLICITUD_INVALIDA"
    assert cotizacion.solicitudes == []


@pytest.mark.parametrize(
    ("estado", "codigo"),
    [
        (404, "PERFIL_NO_ENCONTRADO"),
        (422, "IDEMPOTENCIA_CONFLICTO"),
        (503, "DEPENDENCIA_NO_DISPONIBLE"),
    ],
)
def test_recalcular_pasa_los_errores_b2(
    cotizacion: CotizacionFalsa, estado: int, codigo: str
) -> None:
    cotizacion.respuesta = httpx.Response(
        estado, json={"codigo": codigo, "mensaje": "m", "detalles": {}}
    )
    respuesta = _recalcular()
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)


def test_recalcular_con_cotizacion_caida_responde_503(cotizacion: CotizacionFalsa) -> None:
    cotizacion.respuesta = httpx.ConnectError("sin conexión")
    respuesta = _recalcular()
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")


def _comparar(perfil_id: str = NUEVO_ID) -> httpx.Response:
    return TestClient(app).get(f"/v1/perfiles/{perfil_id}/comparacion", headers=ASESOR)


def test_comparacion_responde_anterior_y_actual(cotizacion: CotizacionFalsa) -> None:
    cotizacion.por_id = {
        NUEVO_ID: httpx.Response(200, json=RECALCULADO),
        PERFIL_ID: httpx.Response(200, json=DEGRADADO),
    }
    respuesta = _comparar()
    assert respuesta.status_code == 200
    assert respuesta.json() == {"anterior": DEGRADADO, "actual": RECALCULADO}
    assert [s.url.path for s in cotizacion.solicitudes] == [
        f"/v1/perfiles/{NUEVO_ID}",
        f"/v1/perfiles/{PERFIL_ID}",
    ]
    assert {s.headers["X-Actor-Id"] for s in cotizacion.solicitudes} == {"sub-asesora"}


def test_comparacion_sin_perfil_anterior_responde_422(cotizacion: CotizacionFalsa) -> None:
    respuesta = _comparar(PERFIL_ID)
    assert respuesta.status_code == 422
    assert respuesta.json()["codigo"] == "SIN_PERFIL_ANTERIOR"
    assert respuesta.json()["correlationId"] == "corr-bff-w11"
    assert len(cotizacion.solicitudes) == 1


@pytest.mark.parametrize("inexistente", [NUEVO_ID, PERFIL_ID], ids=["actual", "anterior"])
def test_comparacion_con_un_perfil_inexistente_responde_404(
    cotizacion: CotizacionFalsa, inexistente: str
) -> None:
    no_existe = httpx.Response(
        404, json={"codigo": "PERFIL_NO_ENCONTRADO", "mensaje": "m", "detalles": {}}
    )
    cotizacion.por_id = {NUEVO_ID: httpx.Response(200, json=RECALCULADO), inexistente: no_existe}
    respuesta = _comparar()
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "PERFIL_NO_ENCONTRADO")


def test_comparacion_con_id_invalido_no_llega_a_cotizacion(cotizacion: CotizacionFalsa) -> None:
    respuesta = _comparar("no-es-uuid")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert cotizacion.solicitudes == []


# --- HU-W31 · marca "revocado durante el perfilamiento" (PR 3.4): solo se transmite --------

REVOCADO_EN_VUELO = {
    **PERFIL,
    "comprobacionFinal": {"decision": "RECHAZADO", "causa": "REVOCADO", "vigenteHasta": None},
    "revocadoDurantePerfilamiento": True,
}
COMPROBADO_VIGENTE = {
    **PERFIL,
    "comprobacionFinal": {
        "decision": "PERMITIDO",
        "causa": None,
        "vigenteHasta": "2027-10-01T00:00:00Z",
    },
    "revocadoDurantePerfilamiento": False,
}


@pytest.mark.parametrize(
    "perfil", [REVOCADO_EN_VUELO, COMPROBADO_VIGENTE], ids=["revocado", "vigente"]
)
def test_crear_y_recalcular_transmiten_la_comprobacion_final(
    cotizacion: CotizacionFalsa, perfil: dict[str, Any]
) -> None:
    cotizacion.respuesta = httpx.Response(201, json=perfil)
    assert _crear().json() == perfil
    assert _recalcular().json() == perfil


def test_consultar_y_comparar_transmiten_la_marca(cotizacion: CotizacionFalsa) -> None:
    actual = {**REVOCADO_EN_VUELO, "id": NUEVO_ID, "perfilAnteriorId": PERFIL_ID}
    cotizacion.por_id = {
        NUEVO_ID: httpx.Response(200, json=actual),
        PERFIL_ID: httpx.Response(200, json=PERFIL),
    }
    consultado = TestClient(app).get(f"/v1/perfiles/{NUEVO_ID}", headers=ASESOR).json()
    assert consultado["revocadoDurantePerfilamiento"] is True
    assert consultado["comprobacionFinal"]["causa"] == "REVOCADO"
    assert _comparar().json() == {"anterior": PERFIL, "actual": actual}


def test_sin_la_marca_de_cotizacion_no_se_inventa(cotizacion: CotizacionFalsa) -> None:
    """cotizacion N-1 no envía los campos: el BFF no responde `false` en su nombre."""
    cuerpo = _crear().json()
    assert not {"comprobacionFinal", "revocadoDurantePerfilamiento"} & set(cuerpo)


# Perfil de cotizacion con null en cada campo que lo admite (C08: sin consentimiento ni fuentes).
CON_NULOS = {
    **PERFIL,
    "estado": "INFORMACION_INSUFICIENTE",
    "nivelRiesgo": None,
    "puntaje": None,
    "confianza": 0.0,
    "factores": [
        {
            **PERFIL["factores"][0],
            "estado": "NO_EVALUADO",
            "contribucion": None,
            "direccion": None,
            "motivo": "NO_AUTORIZADO",
        }
    ],
    "ajustes": [{"codigo": "X", "efecto": "SUBE", "nivel": None}],
    "datosRequeridos": [{"campo": "moras_12m", "fuente": None, "motivo": "NO_AUTORIZADO"}],
    "accionSugerida": "COMPLETAR_CONSENTIMIENTO",
    "consentimiento": None,
    "fuentes": [
        {
            **PERFIL["fuentes"][0],
            "estado": "NO_AUTORIZADA",
            "versionFuente": None,
            "confianza": None,
            "consultadaEn": None,
            "duracionMs": None,
        }
    ],
    "fuentesConsultadas": 0,
    "perfilAnteriorId": None,
}


def _claves(valor: Any, prefijo: str = "") -> set[str]:
    """Rutas de todas las claves, también las que valen null; los ítems de lista por índice."""
    if isinstance(valor, dict):
        return {f"{prefijo}.{k}" for k in valor} | {
            r for k, v in valor.items() for r in _claves(v, f"{prefijo}.{k}")
        }
    if isinstance(valor, list):
        return {r for i, v in enumerate(valor) for r in _claves(v, f"{prefijo}[{i}]")}
    return set()


@pytest.mark.parametrize(
    "perfil",
    [CON_NULOS, {**CON_NULOS, "comprobacionFinal": None, "revocadoDurantePerfilamiento": False}],
    ids=["cotizacion-anterior-a-3.4", "cotizacion-con-3.4"],
)
def test_las_rutas_del_perfil_conservan_exactamente_las_claves_de_cotizacion(
    cotizacion: CotizacionFalsa, perfil: dict[str, Any]
) -> None:
    """`response_model_exclude_unset` no quita claves con null ni añade las no enviadas."""
    recalculado = {**perfil, "id": NUEVO_ID, "perfilAnteriorId": PERFIL_ID}
    cotizacion.respuesta = httpx.Response(201, json=perfil)
    cotizacion.por_id = {
        PERFIL_ID: httpx.Response(200, json=perfil),
        NUEVO_ID: httpx.Response(200, json=recalculado),
    }
    respuestas = {
        "crear": (_crear(), perfil),
        "consultar": (TestClient(app).get(f"/v1/perfiles/{PERFIL_ID}", headers=ASESOR), perfil),
        "recalcular": (_recalcular(), perfil),
        "comparar": (_comparar(), {"anterior": perfil, "actual": recalculado}),
    }
    for nombre, (respuesta, enviado) in respuestas.items():
        assert respuesta.status_code in (200, 201), nombre
        assert _claves(respuesta.json()) == _claves(enviado), nombre
        assert respuesta.json() == enviado, nombre
