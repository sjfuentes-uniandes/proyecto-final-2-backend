"""API POST /v1/desembolsos y GET /v1/desembolsos/{referencia}: cada correspondencia de errores."""

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from adaptador_pagos.config import Settings
from adaptador_pagos.main import crear_app
from fastapi.testclient import TestClient

REFERENCIA = "IND-88-2026-0442-01"
BENEFICIARIO = "ben-sint-001"
CORRELACION = "corr-api-w23"
CONSULTA = f"/v1/desembolsos/{REFERENCIA}"

Manejador = Callable[[httpx.Request], httpx.Response | Exception]


def _referencia(request: httpx.Request) -> str:
    if request.method == "POST":
        return str(json.loads(request.content)["referencia"])
    return parse_qs(request.url.query.decode())["referencia"][0]


def _confirmado(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "referencia": _referencia(request),
            "estado": "CONFIRMADO",
            "transaccionId": "TRX-SINT-1",
            "motivo": None,
        },
    )


def _rechazado(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "referencia": _referencia(request),
            "estado": "RECHAZADO",
            "transaccionId": None,
            "motivo": "BENEFICIARIO_NO_HABILITADO",
        },
    )


@pytest.fixture
def cliente_con() -> Iterator[Callable[[Manejador], tuple[TestClient, list[httpx.Request]]]]:
    abiertos: list[TestClient] = []

    def crear(manejador: Manejador) -> tuple[TestClient, list[httpx.Request]]:
        recibidas: list[httpx.Request] = []

        async def registrar(request: httpx.Request) -> httpx.Response:
            recibidas.append(request)
            respuesta = manejador(request)
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        app = crear_app(Settings(), transport=httpx.MockTransport(registrar))
        cliente = TestClient(app, headers={"X-Correlation-Id": CORRELACION})
        cliente.__enter__()
        abiertos.append(cliente)
        return cliente, recibidas

    yield crear
    for cliente in abiertos:
        cliente.__exit__(None, None, None)


_FALTA = object()


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "referencia": REFERENCIA,
        "monto": "2500000.00",
        "moneda": "COP",
        "beneficiarioRef": BENEFICIARIO,
    }
    cuerpo.update(cambios)
    return {clave: valor for clave, valor in cuerpo.items() if valor is not _FALTA}


def _ordenar(cliente: TestClient, **cambios: Any) -> httpx.Response:
    return cliente.post("/v1/desembolsos", json=_cuerpo(**cambios))


def _consultar(cliente: TestClient, beneficiario: str | None = BENEFICIARIO) -> httpx.Response:
    params = {} if beneficiario is None else {"beneficiarioRef": beneficiario}
    return cliente.get(CONSULTA, params=params)


LLAMADAS = {"desembolso": _ordenar, "consulta": _consultar}


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


# --- 200 -----------------------------------------------------------------------------------
def test_desembolso_confirmado(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_confirmado)
    respuesta = _ordenar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "referencia": REFERENCIA,
        "estado": "CONFIRMADO",
        "transaccionId": "TRX-SINT-1",
        "motivo": None,
    }
    (request,) = recibidas
    assert request.method == "POST"
    assert request.url.path == "/pagos/v1/desembolsos"
    assert request.headers["X-Api-Key"] == "local-sintetico"
    assert request.headers["Idempotency-Key"] == REFERENCIA
    assert request.headers["X-Correlation-Id"] == CORRELACION
    assert json.loads(request.content) == {
        "referencia": REFERENCIA,
        "monto": "2500000.00",
        "moneda": "COP",
        "beneficiario": BENEFICIARIO,
    }


def test_desembolso_rechazado(cliente_con: Any) -> None:
    cliente, _ = cliente_con(_rechazado)
    respuesta = _ordenar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "referencia": REFERENCIA,
        "estado": "RECHAZADO",
        "transaccionId": None,
        "motivo": "BENEFICIARIO_NO_HABILITADO",
    }


@pytest.mark.parametrize("monto", ["1", "0.01", "2500000", "2500000.5", "2500000.50"])
def test_el_monto_se_envia_como_texto_decimal_sin_cambios(cliente_con: Any, monto: str) -> None:
    cliente, recibidas = cliente_con(_confirmado)
    assert _ordenar(cliente, monto=monto).status_code == 200
    assert json.loads(recibidas[0].content)["monto"] == monto


@pytest.mark.parametrize(
    ("manejador", "estado"), [(_confirmado, "CONFIRMADO"), (_rechazado, "RECHAZADO")]
)
def test_consulta(cliente_con: Any, manejador: Manejador, estado: str) -> None:
    cliente, recibidas = cliente_con(manejador)
    respuesta = _consultar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["referencia"] == REFERENCIA
    assert respuesta.json()["estado"] == estado
    (request,) = recibidas
    assert request.method == "GET"
    assert request.url.path == "/pagos/v1/desembolsos"
    assert parse_qs(request.url.query.decode()) == {
        "referencia": [REFERENCIA],
        "beneficiario": [BENEFICIARIO],
    }
    assert request.headers["X-Correlation-Id"] == CORRELACION


def test_consulta_404_desembolso_no_encontrado(cliente_con: Any) -> None:
    cliente, _ = cliente_con(
        lambda _: httpx.Response(404, json={"error": "desembolso_no_encontrado"})
    )
    cuerpo = _error(_consultar(cliente), 404, "DESEMBOLSO_NO_ENCONTRADO")
    assert cuerpo["detalles"] == {}


# --- 422: entrada inválida, sin llamar al proveedor ------------------------------------------
@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"monto": "0"}, "monto"),
        ({"monto": "0.00"}, "monto"),
        ({"monto": "-1"}, "monto"),
        ({"monto": "1.234"}, "monto"),
        ({"monto": "1e3"}, "monto"),
        ({"monto": ""}, "monto"),
        ({"monto": 2500000}, "monto"),
        ({"monto": _FALTA}, "monto"),
        ({"moneda": "USD"}, "moneda"),
        ({"moneda": _FALTA}, "moneda"),
        ({"referencia": "no-es-una-referencia"}, "referencia"),
        ({"referencia": "IND-0442-01"}, "referencia"),
        ({"referencia": _FALTA}, "referencia"),
        ({"beneficiarioRef": ""}, "beneficiarioRef"),
        ({"beneficiarioRef": "x" * 129}, "beneficiarioRef"),
        ({"beneficiarioRef": _FALTA}, "beneficiarioRef"),
    ],
)
def test_desembolso_422_solicitud_invalida(
    cliente_con: Any, cambios: dict[str, Any], campo: str
) -> None:
    cliente, recibidas = cliente_con(_confirmado)
    cuerpo = _error(_ordenar(cliente, **cambios), 422, "SOLICITUD_INVALIDA")
    assert cuerpo["detalles"]["campos"] == [campo]
    assert recibidas == []


@pytest.mark.parametrize(
    ("ruta", "params", "campo"),
    [
        ("/v1/desembolsos/no-es-una-referencia", {"beneficiarioRef": BENEFICIARIO}, "referencia"),
        (CONSULTA, {"beneficiarioRef": ""}, "beneficiarioRef"),
        (CONSULTA, {}, "beneficiarioRef"),
    ],
)
def test_consulta_422_solicitud_invalida(
    cliente_con: Any, ruta: str, params: dict[str, str], campo: str
) -> None:
    cliente, recibidas = cliente_con(_confirmado)
    cuerpo = _error(cliente.get(ruta, params=params), 422, "SOLICITUD_INVALIDA")
    assert cuerpo["detalles"]["campos"] == [campo]
    assert recibidas == []


# --- Correspondencia de los fallos del proveedor (las dos rutas) -----------------------------
@pytest.mark.parametrize("ruta", LLAMADAS)
def test_504_tiempo_agotado(cliente_con: Any, ruta: str) -> None:
    cliente, _ = cliente_con(lambda r: httpx.ReadTimeout("lento", request=r))
    _error(LLAMADAS[ruta](cliente), 504, "TIEMPO_AGOTADO")


@pytest.mark.parametrize("ruta", LLAMADAS)
@pytest.mark.parametrize(
    "manejador",
    [
        pytest.param(lambda _: httpx.Response(503, json={"error": "unavailable"}), id="503"),
        pytest.param(lambda _: httpx.Response(500), id="500"),
        pytest.param(lambda r: httpx.ConnectError("caído", request=r), id="conexion"),
    ],
)
def test_503_fuente_no_disponible(cliente_con: Any, ruta: str, manejador: Manejador) -> None:
    cliente, recibidas = cliente_con(manejador)
    cuerpo = _error(LLAMADAS[ruta](cliente), 503, "FUENTE_NO_DISPONIBLE")
    assert cuerpo["detalles"] == {"motivo": "ERROR", "circuito": "CERRADO"}
    # Sin reintentos dentro del adaptador.
    assert len(recibidas) == 1


@pytest.mark.parametrize("ruta", LLAMADAS)
@pytest.mark.parametrize(
    "respuesta",
    [
        pytest.param(httpx.Response(200, text='{"estado": '), id="malformado"),
        pytest.param(httpx.Response(200, json={"estado": "PENDIENTE"}), id="fuera-de-forma"),
        pytest.param(
            httpx.Response(404, json={"error": "referencia_no_reconocida"}),
            id="404-referencia-no-reconocida",
        ),
        pytest.param(httpx.Response(400, json={"error": "x"}), id="400"),
        pytest.param(httpx.Response(409), id="409"),
    ],
)
def test_502_respuesta_invalida(cliente_con: Any, ruta: str, respuesta: httpx.Response) -> None:
    cliente, recibidas = cliente_con(lambda _: respuesta)
    cuerpo = _error(LLAMADAS[ruta](cliente), 502, "RESPUESTA_INVALIDA")
    assert cuerpo["detalles"] == {}
    assert len(recibidas) == 1


def test_desembolso_404_no_encontrado_es_502(cliente_con: Any) -> None:
    """Solo la consulta traduce `desembolso_no_encontrado`: en la orden no es un rechazo."""
    cliente, _ = cliente_con(
        lambda _: httpx.Response(404, json={"error": "desembolso_no_encontrado"})
    )
    _error(_ordenar(cliente), 502, "RESPUESTA_INVALIDA")


def test_confirmado_de_otra_referencia_es_502(cliente_con: Any) -> None:
    def otra(request: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(_confirmado(request).content)
        cuerpo["referencia"] = "IND-88-2026-9999-01"
        return httpx.Response(200, json=cuerpo)

    cliente, _ = cliente_con(otra)
    _error(_ordenar(cliente), 502, "RESPUESTA_INVALIDA")


# --- Timeout: ALLY_PAGOS_TIMEOUT_MS como corte duro ------------------------------------------
@pytest.mark.parametrize(("variable", "esperado"), [(None, 5.0), ("1200", 1.2), ("8000", 8.0)])
@pytest.mark.parametrize("ruta", LLAMADAS)
def test_el_timeout_configurado_es_el_corte_duro(
    monkeypatch: pytest.MonkeyPatch, ruta: str, variable: str | None, esperado: float
) -> None:
    if variable is None:
        monkeypatch.delenv("ALLY_PAGOS_TIMEOUT_MS", raising=False)
    else:
        monkeypatch.setenv("ALLY_PAGOS_TIMEOUT_MS", variable)
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return _confirmado(request)

    app = crear_app(Settings(), transport=httpx.MockTransport(manejador))
    with TestClient(app) as cliente:
        assert LLAMADAS[ruta](cliente).status_code == 200
    assert recibidas[0].extensions["timeout"]["read"] == esperado


def test_el_corte_lo_aplica_la_proteccion_y_no_solo_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLY_PAGOS_TIMEOUT_MS", "100")

    async def colgado(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _confirmado(request)

    app = crear_app(Settings(), transport=httpx.MockTransport(colgado))
    with TestClient(app) as cliente:
        assert _ordenar(cliente).json()["codigo"] == "TIEMPO_AGOTADO"


# --- Logs: sin secreto, sin beneficiario y sin cuerpos ---------------------------------------
SECRETO = "secreto-centinela-pagos"
BENEFICIARIO_CENTINELA = "ben-sint-centinela-w23"
MONTO_CENTINELA = "98765.43"
TX_CENTINELA = "TRX-CENTINELA-W23"
MOTIVO_CENTINELA = "MOTIVO_CENTINELA_W23"


def _eco_centinela(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "referencia": _referencia(request),
            "estado": "CONFIRMADO",
            "transaccionId": TX_CENTINELA,
            "motivo": MOTIVO_CENTINELA,
        },
    )


@pytest.mark.parametrize(
    ("ruta", "manejador"),
    [
        pytest.param("desembolso", _eco_centinela, id="desembolso-200"),
        pytest.param("consulta", _eco_centinela, id="consulta-200"),
        pytest.param(
            "desembolso", lambda r: httpx.ReadTimeout("lento", request=r), id="desembolso-504"
        ),
        pytest.param("consulta", lambda _: httpx.Response(503), id="consulta-503"),
        pytest.param(
            "desembolso",
            lambda _: httpx.Response(200, text=f'{{"x": "{TX_CENTINELA}"'),
            id="desembolso-502",
        ),
        pytest.param(
            "consulta",
            lambda _: httpx.Response(404, json={"error": "desembolso_no_encontrado"}),
            id="consulta-404",
        ),
    ],
)
def test_los_logs_no_llevan_secreto_beneficiario_ni_cuerpos(
    monkeypatch: pytest.MonkeyPatch,
    capturar_logs: Callable[..., Any],
    cliente_con: Any,
    ruta: str,
    manejador: Manejador,
) -> None:
    salida = capturar_logs()
    monkeypatch.setenv(
        "ALLY_PAGOS_CREDENTIALS", json.dumps({"client_id": "id", "client_secret": SECRETO})
    )
    cliente, _ = cliente_con(manejador)
    if ruta == "desembolso":
        cliente.post(
            "/v1/desembolsos",
            json=_cuerpo(beneficiarioRef=BENEFICIARIO_CENTINELA, monto=MONTO_CENTINELA),
        )
    else:
        cliente.get(CONSULTA, params={"beneficiarioRef": BENEFICIARIO_CENTINELA})
    texto = salida.getvalue()
    for centinela in (
        SECRETO,
        BENEFICIARIO_CENTINELA,
        MONTO_CENTINELA,
        TX_CENTINELA,
        MOTIVO_CENTINELA,
    ):
        assert centinela not in texto
