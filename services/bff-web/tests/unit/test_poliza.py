"""UT-W17 (bff-web, 17.11 y 17.11c): la póliza y la URL de su documento en el portal (HU-W17) con
polizas y clientes simulados.

Solo `asesores` consulta (los demás grupos, 403 sin llamar a polizas); actor, rol `ASESOR` y
correlación hacia polizas; los 4xx que polizas promete para cada ruta pasan tal cual y todo lo demás
es 503 SERVICIO_NO_DISPONIBLE sin detalles (falla cerrada). La URL prefirmada nunca va a los logs.

17.11c: la póliza ACTIVA sale con `titularNombre` (de clientes, como `clienteNombre` en la
suscripción) y sin el `clienteId`. EMITIENDO no llama a clientes (el portal la consulta en bucle). Si
clientes falla, tarda o no da el nombre, `titularNombre` es null y la póliza sale igual.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import polizas as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.polizas import PolizasCliente
from bff_web.application.poliza import Polizas
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
COTIZACION_ID = "00000000-0000-4000-a000-000000000001"
POLIZA_ID = "00000000-0000-4000-9000-000000000001"
CLIENTE_ID = "00000000-0000-4000-8000-000000000001"
NOMBRE = "Cliente Sintético 01"
RUTA_POLIZA = f"/v1/cotizaciones/{COTIZACION_ID}/poliza"
RUTA_DOCUMENTO = f"/v1/polizas/{POLIZA_ID}/documento"
# Lo que responde polizas (con el titular desde la 17.11c).
EMITIENDO: dict[str, Any] = {
    "polizaId": POLIZA_ID,
    "clienteId": CLIENTE_ID,
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
# GET /v1/clientes/{cliente_id} de clientes (`ClienteLecturaDto`, clientes.yaml).
CLIENTE = {
    "clienteId": CLIENTE_ID,
    "estado": "VERIFICADO",
    "verificadoEn": "2026-10-20T14:00:00Z",
    "proveedor": "KYC",
    "referencia": "kyc-ref-001",
    "nombreCompleto": NOMBRE,
}


def _vista(poliza: dict[str, Any], nombre: str | None) -> dict[str, Any]:
    """Lo que recibe el portal: la póliza sin el `clienteId` y con el nombre del titular."""
    return {**{k: v for k, v in poliza.items() if k != "clienteId"}, "titularNombre": nombre}


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
    """polizas: la póliza por la ruta 1 y el enlace por la ruta 2, salvo que se cambie `responder`.
    clientes: el `ClienteLecturaDto` del titular, salvo que se cambie `clientes`."""

    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.a_clientes: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = self._por_ruta
        self.clientes: Callable[[httpx.Request], Any] = lambda _: httpx.Response(200, json=CLIENTE)

    @staticmethod
    def _por_ruta(pedido: httpx.Request) -> httpx.Response:
        if pedido.url.path.endswith("/documento"):
            return httpx.Response(200, json=ENLACE)
        return httpx.Response(200, json=EMITIENDO)

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)

    async def responder_clientes(self, pedido: httpx.Request) -> httpx.Response:
        self.a_clientes.append(pedido)
        respuesta = self.clientes(pedido)
        return await respuesta if asyncio.iscoroutine(respuesta) else respuesta


@pytest.fixture
def polizas(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulado]:
    simulado = Simulado()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = PolizasCliente("http://polizas:8080", 1500, httpx.MockTransport(simulado))
    clientes = ClientesCliente(
        "http://clientes:8080", 5000, httpx.MockTransport(simulado.responder_clientes)
    )
    # El nombre con 200 ms, como la suscripción en sus pruebas (por omisión, 500 ms).
    app.dependency_overrides[ruta.polizas] = lambda: Polizas(cliente, clientes, 200)
    yield simulado
    app.dependency_overrides.clear()


def _get(ruta_: str, encabezados: dict[str, str] = ASESOR) -> httpx.Response:
    return TestClient(app).get(ruta_, headers=encabezados)


RUTAS = pytest.mark.parametrize("ruta_", [RUTA_POLIZA, RUTA_DOCUMENTO], ids=["poliza", "documento"])


def _json(estado: int, cuerpo: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(estado, json=cuerpo)


# --- Lo que el portal recibe ------------------------------------------------------------------


def test_la_poliza_activa_sale_con_el_nombre_del_titular(polizas: Simulado) -> None:
    polizas.responder = _json(200, ACTIVA)
    respuesta = _get(RUTA_POLIZA)
    assert (respuesta.status_code, respuesta.json()) == (200, _vista(ACTIVA, NOMBRE))
    # El titular de la póliza, pedido una vez a clientes como el asesor (como la suscripción).
    [pedido] = polizas.a_clientes
    assert (pedido.method, pedido.url.path) == ("GET", f"/v1/clientes/{CLIENTE_ID}")
    assert (pedido.headers["X-Actor-Id"], pedido.headers["X-Actor-Rol"]) == ("sub-asesor", "ASESOR")


def test_el_nombre_es_el_del_titular_de_esa_poliza(polizas: Simulado) -> None:
    otro = "00000000-0000-4000-8000-000000000004"
    nombres = {CLIENTE_ID: NOMBRE, otro: "Cliente Sintético 04"}
    polizas.responder = _json(200, {**ACTIVA, "clienteId": otro})

    def por_id(pedido: httpx.Request) -> httpx.Response:
        cliente_id = pedido.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200, json={**CLIENTE, "clienteId": cliente_id, "nombreCompleto": nombres[cliente_id]}
        )

    polizas.clientes = por_id
    assert _get(RUTA_POLIZA).json()["titularNombre"] == "Cliente Sintético 04"


def test_la_poliza_emitiendo_no_llama_a_clientes(polizas: Simulado) -> None:
    """El portal la consulta en bucle mientras se emite: sin nombre y sin llamar a clientes."""
    respuesta = _get(RUTA_POLIZA)
    assert (respuesta.status_code, respuesta.json()) == (200, _vista(EMITIENDO, None))
    assert polizas.a_clientes == []


async def _tarda(_: httpx.Request) -> httpx.Response:
    await asyncio.sleep(1)  # más que el timeout del nombre (200 ms)
    return httpx.Response(200, json=CLIENTE)


def _clientes_lento(pedido: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("clientes no respondió a tiempo", request=pedido)


def _clientes_caido(pedido: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("clientes caído", request=pedido)


FALLOS_CLIENTES: dict[str, Callable[[httpx.Request], Any]] = {
    "503": _json(503, {"codigo": "DEPENDENCIA_NO_DISPONIBLE", "detalles": {"x": 1}}),
    "500": _json(500, {"codigo": "ERROR_INTERNO"}),
    "404": _json(404, {"codigo": "CLIENTE_NO_ENCONTRADO", "mensaje": "m", "detalles": {}}),
    "nombre-nulo": _json(200, {**CLIENTE, "nombreCompleto": None}),
    "sin-nombre": _json(200, {k: v for k, v in CLIENTE.items() if k != "nombreCompleto"}),
    "nombre-no-texto": _json(200, {**CLIENTE, "nombreCompleto": 1}),
    "no-objeto": _json(200, [CLIENTE]),
    "no-json": lambda _: httpx.Response(200, text="<html>"),
    "tarda": _tarda,
    "timeout": _clientes_lento,
    "caido": _clientes_caido,
}


@pytest.mark.parametrize("fallo", list(FALLOS_CLIENTES))
def test_sin_nombre_si_clientes_no_lo_da_la_poliza_sale_igual(
    polizas: Simulado, fallo: str
) -> None:
    """Nunca un 503 por el nombre."""
    polizas.responder = _json(200, ACTIVA)
    polizas.clientes = FALLOS_CLIENTES[fallo]
    respuesta = _get(RUTA_POLIZA)
    assert (respuesta.status_code, respuesta.json()) == (200, _vista(ACTIVA, None))
    assert len(polizas.a_clientes) == 1


@pytest.mark.parametrize(
    "poliza",
    [
        {k: v for k, v in ACTIVA.items() if k != "clienteId"},
        {**ACTIVA, "clienteId": None},
        {**ACTIVA, "clienteId": "no-es-uuid"},
    ],
    ids=["sin-cliente", "cliente-nulo", "cliente-no-uuid"],
)
def test_sin_un_titular_valido_no_hay_nombre_ni_llamada(
    polizas: Simulado, poliza: dict[str, Any]
) -> None:
    """El `clienteId` solo sirve para el nombre: sin él, la póliza sale igual, sin nombre."""
    polizas.responder = _json(200, poliza)
    respuesta = _get(RUTA_POLIZA)
    assert (respuesta.status_code, respuesta.json()) == (200, _vista(ACTIVA, None))
    assert polizas.a_clientes == []


def test_el_asesor_obtiene_la_url_del_documento(polizas: Simulado) -> None:
    respuesta = _get(RUTA_DOCUMENTO)
    assert (respuesta.status_code, respuesta.json()) == (200, ENLACE)
    assert polizas.a_clientes == []


def test_los_campos_internos_de_polizas_no_llegan_al_portal(polizas: Simulado) -> None:
    """Si polizas agregara campos (contrato aditivo), el portal recibe solo los del DTO: tampoco
    el `clienteId`."""
    polizas.responder = lambda pedido: httpx.Response(
        200,
        json={**ENLACE, "clave": "polizas/x.pdf"}
        if pedido.url.path.endswith("/documento")
        else {**ACTIVA, "socioId": "00000000-0000-4000-8000-000000000009"},
    )
    cuerpo = _get(RUTA_POLIZA).json()
    assert cuerpo == _vista(ACTIVA, NOMBRE)
    assert not {"clienteId", "socioId"} & set(cuerpo)
    assert CLIENTE_ID not in str(cuerpo)
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
        # 17.11c: ni el titular ni su nombre, tampoco si clientes falla.
        for fallo in ("404", "500", "caido", "tarda"):
            polizas.clientes = FALLOS_CLIENTES[fallo]
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
        CLIENTE_ID,
        NOMBRE,
    ):
        assert centinela not in texto, centinela


def test_el_servicio_por_omision_usa_la_configuracion() -> None:
    ruta.polizas.cache_clear()
    servicio = ruta.polizas()
    assert servicio.polizas.url == settings.polizas_url.rstrip("/")
    assert servicio.polizas.timeout == settings.polizas_timeout_ms / 1000
    # 17.11c: el nombre del titular, con el cliente y el timeout de la suscripción.
    assert servicio.clientes.url == settings.clientes_url.rstrip("/")
    assert servicio.clientes.timeout == settings.clientes_timeout_ms / 1000
    assert servicio._nombre_timeout_s == settings.nombre_cliente_timeout_ms / 1000
    ruta.polizas.cache_clear()
