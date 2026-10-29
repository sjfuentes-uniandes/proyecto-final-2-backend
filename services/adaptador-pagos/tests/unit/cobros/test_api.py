"""API POST /v1/cobros y GET /v1/cobros/{claveRecaudo}: cada correspondencia de errores."""

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
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

CLAVE = "7d3f6a2e-0c1b-4b8e-9f00-000000000a01"
PAGADOR = "00000000-0000-4000-8000-000000000001"
CORRELACION = "corr-api-w16"
CONSULTA = f"/v1/cobros/{CLAVE}"

Manejador = Callable[[httpx.Request], httpx.Response | Exception]


def _referencia(request: httpx.Request) -> str:
    if request.method == "POST":
        return str(json.loads(request.content)["referencia"])
    return parse_qs(request.url.query.decode())["referencia"][0]


def _aprobado(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "referencia": _referencia(request),
            "estado": "APROBADO",
            "transaccionId": "TX-SINT-C01",
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
            "motivo": "FONDOS_INSUFICIENTES",
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


def _cuerpo(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "claveRecaudo": CLAVE,
        "monto": "64638.00",
        "moneda": "COP",
        "pagadorRef": PAGADOR,
    }
    cuerpo.update(cambios)
    return {clave: valor for clave, valor in cuerpo.items() if valor is not _FALTA}


_FALTA = object()


def _cobrar(cliente: TestClient, **cambios: Any) -> httpx.Response:
    return cliente.post("/v1/cobros", json=_cuerpo(**cambios))


def _consultar(cliente: TestClient, pagador: str | None = PAGADOR) -> httpx.Response:
    params = {} if pagador is None else {"pagadorRef": pagador}
    return cliente.get(CONSULTA, params=params)


LLAMADAS = {"cobro": _cobrar, "consulta": _consultar}


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


# --- 200 -----------------------------------------------------------------------------------
def test_cobro_aprobado(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_aprobado)
    respuesta = _cobrar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "claveRecaudo": CLAVE,
        "estado": "APROBADO",
        "transaccionId": "TX-SINT-C01",
        "motivo": None,
    }
    (request,) = recibidas
    assert request.method == "POST"
    assert request.url.path == "/pagos/v1/cobros"
    assert request.headers["X-Api-Key"] == "local-sintetico"
    assert request.headers["Idempotency-Key"] == CLAVE
    assert request.headers["X-Correlation-Id"] == CORRELACION
    assert json.loads(request.content) == {
        "referencia": CLAVE,
        "monto": "64638.00",
        "moneda": "COP",
        "pagador": PAGADOR,
    }


def test_cobro_rechazado(cliente_con: Any) -> None:
    cliente, _ = cliente_con(_rechazado)
    respuesta = _cobrar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "claveRecaudo": CLAVE,
        "estado": "RECHAZADO",
        "transaccionId": None,
        "motivo": "FONDOS_INSUFICIENTES",
    }


@pytest.mark.parametrize("monto", ["1", "0.01", "64638", "64638.5", "64638.50"])
def test_el_monto_se_envia_como_texto_decimal_sin_cambios(cliente_con: Any, monto: str) -> None:
    cliente, recibidas = cliente_con(_aprobado)
    assert _cobrar(cliente, monto=monto).status_code == 200
    assert json.loads(recibidas[0].content)["monto"] == monto


@pytest.mark.parametrize(
    ("manejador", "estado"), [(_aprobado, "APROBADO"), (_rechazado, "RECHAZADO")]
)
def test_consulta(cliente_con: Any, manejador: Manejador, estado: str) -> None:
    cliente, recibidas = cliente_con(manejador)
    respuesta = _consultar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["claveRecaudo"] == CLAVE
    assert respuesta.json()["estado"] == estado
    (request,) = recibidas
    assert request.method == "GET"
    assert request.url.path == "/pagos/v1/cobros"
    assert parse_qs(request.url.query.decode()) == {"referencia": [CLAVE], "pagador": [PAGADOR]}
    assert request.headers["X-Correlation-Id"] == CORRELACION


def test_consulta_404_cobro_no_encontrado(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda _: httpx.Response(404, json={"error": "cobro_no_encontrado"}))
    cuerpo = _error(_consultar(cliente), 404, "COBRO_NO_ENCONTRADO")
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
        ({"monto": "abc"}, "monto"),
        ({"monto": ""}, "monto"),
        ({"monto": 64638}, "monto"),
        ({"monto": 64638.5}, "monto"),
        ({"monto": _FALTA}, "monto"),
        ({"moneda": "USD"}, "moneda"),
        ({"moneda": "cop"}, "moneda"),
        ({"moneda": _FALTA}, "moneda"),
        ({"claveRecaudo": "no-uuid"}, "claveRecaudo"),
        ({"claveRecaudo": _FALTA}, "claveRecaudo"),
        ({"pagadorRef": "no-uuid"}, "pagadorRef"),
        ({"pagadorRef": _FALTA}, "pagadorRef"),
    ],
)
def test_cobro_422_solicitud_invalida(
    cliente_con: Any, cambios: dict[str, Any], campo: str
) -> None:
    cliente, recibidas = cliente_con(_aprobado)
    cuerpo = _error(_cobrar(cliente, **cambios), 422, "SOLICITUD_INVALIDA")
    assert cuerpo["detalles"]["campos"] == [campo]
    assert recibidas == []


@pytest.mark.parametrize(
    ("ruta", "params", "campo"),
    [
        ("/v1/cobros/no-uuid", {"pagadorRef": PAGADOR}, "claveRecaudo"),
        (CONSULTA, {"pagadorRef": "no-uuid"}, "pagadorRef"),
        (CONSULTA, {}, "pagadorRef"),
    ],
)
def test_consulta_422_solicitud_invalida(
    cliente_con: Any, ruta: str, params: dict[str, str], campo: str
) -> None:
    cliente, recibidas = cliente_con(_aprobado)
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
        pytest.param(httpx.Response(302, headers={"Location": "/otra"}), id="302"),
    ],
)
def test_502_respuesta_invalida(cliente_con: Any, ruta: str, respuesta: httpx.Response) -> None:
    cliente, recibidas = cliente_con(lambda _: respuesta)
    cuerpo = _error(LLAMADAS[ruta](cliente), 502, "RESPUESTA_INVALIDA")
    assert cuerpo["detalles"] == {}
    assert len(recibidas) == 1


def test_cobro_404_cobro_no_encontrado_es_502(cliente_con: Any) -> None:
    """Solo la consulta traduce `cobro_no_encontrado`: en el cobro no es un rechazo."""
    cliente, _ = cliente_con(lambda _: httpx.Response(404, json={"error": "cobro_no_encontrado"}))
    _error(_cobrar(cliente), 502, "RESPUESTA_INVALIDA")


def test_aprobado_de_otra_referencia_es_502(cliente_con: Any) -> None:
    def otra(request: httpx.Request) -> httpx.Response:
        respuesta = _aprobado(request)
        cuerpo = json.loads(respuesta.content)
        cuerpo["referencia"] = "00000000-0000-4000-8000-0000000000ff"
        return httpx.Response(200, json=cuerpo)

    cliente, _ = cliente_con(otra)
    _error(_cobrar(cliente), 502, "RESPUESTA_INVALIDA")


# --- Timeout: ALLY_PAGOS_TIMEOUT_MS como corte duro (no los 700 ms por defecto) ---------------
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
        return _aprobado(request)

    app = crear_app(Settings(), transport=httpx.MockTransport(manejador))
    with TestClient(app) as cliente:
        assert LLAMADAS[ruta](cliente).status_code == 200
    assert recibidas[0].extensions["timeout"]["read"] == esperado


def test_el_corte_lo_aplica_la_proteccion_y_no_solo_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un proveedor que no respeta el timeout de httpx también se corta (asyncio.wait_for)."""
    monkeypatch.setenv("ALLY_PAGOS_TIMEOUT_MS", "100")

    async def colgado(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _aprobado(request)

    app = crear_app(Settings(), transport=httpx.MockTransport(colgado))
    with TestClient(app) as cliente:
        assert _cobrar(cliente).json()["codigo"] == "TIEMPO_AGOTADO"


# --- Logs: sin secreto, sin pagador y sin cuerpos -------------------------------------------
SECRETO = "secreto-centinela-pagos"
PAGADOR_CENTINELA = "00000000-0000-4000-8000-00000000c0de"
MONTO_CENTINELA = "98765.43"
TX_CENTINELA = "TX-CENTINELA-W16"
MOTIVO_CENTINELA = "MOTIVO_CENTINELA_W16"


def _eco_centinela(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "referencia": _referencia(request),
            "estado": "APROBADO",
            "transaccionId": TX_CENTINELA,
            "motivo": MOTIVO_CENTINELA,
        },
    )


@pytest.mark.parametrize(
    ("ruta", "manejador"),
    [
        pytest.param("cobro", _eco_centinela, id="cobro-200"),
        pytest.param("consulta", _eco_centinela, id="consulta-200"),
        pytest.param("cobro", lambda r: httpx.ReadTimeout("lento", request=r), id="cobro-504"),
        pytest.param("consulta", lambda _: httpx.Response(503), id="consulta-503"),
        pytest.param(
            "cobro", lambda _: httpx.Response(200, text=f'{{"x": "{TX_CENTINELA}"'), id="cobro-502"
        ),
        pytest.param(
            "consulta",
            lambda _: httpx.Response(404, json={"error": "cobro_no_encontrado"}),
            id="consulta-404",
        ),
    ],
)
def test_logs_sin_secreto_pagador_ni_cuerpos(
    monkeypatch: pytest.MonkeyPatch,
    capturar_logs: Callable[..., Any],
    ruta: str,
    manejador: Manejador,
) -> None:
    monkeypatch.setenv(
        "ALLY_PAGOS_CREDENTIALS",
        json.dumps({"client_id": "id-centinela", "client_secret": SECRETO}),
    )
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    recibidas: list[httpx.Request] = []

    async def registrar(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        respuesta = manejador(request)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    app = crear_app(
        Settings(), transport=httpx.MockTransport(registrar), span_exporter=InMemorySpanExporter()
    )
    salida = capturar_logs()  # después de crear la app: instrumentar() reconfigura el logging
    with TestClient(app) as cliente:
        if ruta == "cobro":
            cliente.post(
                "/v1/cobros", json=_cuerpo(monto=MONTO_CENTINELA, pagadorRef=PAGADOR_CENTINELA)
            )
        else:
            cliente.get(CONSULTA, params={"pagadorRef": PAGADOR_CENTINELA})
    assert recibidas[0].headers["X-Api-Key"] == SECRETO  # el secreto sí llega al proveedor
    texto = salida.getvalue()
    operacion = "cobro.pagos" if ruta == "cobro" else "conciliacion.pagos"
    assert f'"operation": "{operacion}"' in texto  # hay logs que revisar
    for centinela in (SECRETO, "id-centinela", PAGADOR_CENTINELA, MONTO_CENTINELA, TX_CENTINELA):
        assert centinela not in texto, centinela
    assert MOTIVO_CENTINELA not in texto


# --- Configuración y ciclo de vida ----------------------------------------------------------
def test_configuracion_de_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los nombres que entrega infra/apps/ecs.tf para el aliado `pagos`."""
    monkeypatch.setenv("ALLY_PAGOS_URL", "http://simulador-aliados:8080/pagos")
    monkeypatch.setenv("ALLY_PAGOS_TIMEOUT_MS", "5000")
    monkeypatch.setenv(
        "ALLY_PAGOS_CREDENTIALS", json.dumps({"client_id": "id", "client_secret": "secreto"})
    )
    settings = Settings()
    assert settings.ally_pagos_url == "http://simulador-aliados:8080/pagos"
    assert settings.ally_pagos_timeout_ms == 5000
    assert settings.ally_pagos_credentials.client_secret.get_secret_value() == "secreto"


def test_credenciales_fuera_de_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ALLY_PAGOS_CREDENTIALS",
        json.dumps({"client_id": "id-centinela-pagos", "client_secret": SECRETO}),
    )
    settings = Settings()
    for texto in (repr(settings), str(settings)):
        assert "id-centinela-pagos" not in texto
        assert SECRETO not in texto


@pytest.mark.parametrize(
    "credenciales",
    ['{"client_id": "solo-id"}', '{"client_secret": "solo-secreto"}', "no-es-json"],
)
def test_credenciales_incompletas_fallan_sin_mostrarlas(
    monkeypatch: pytest.MonkeyPatch, credenciales: str
) -> None:
    from pydantic import ValidationError
    from pydantic_settings import SettingsError

    monkeypatch.setenv("ALLY_PAGOS_CREDENTIALS", credenciales)
    with pytest.raises((ValidationError, SettingsError)) as error:
        Settings()
    assert "solo-secreto" not in str(error.value)
    assert "solo-id" not in str(error.value)


def test_health_sin_dependencias(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_aprobado)
    assert cliente.get("/health").json() == {"status": "ok"}
    assert recibidas == []


def test_el_lifespan_cierra_el_cliente() -> None:
    app = crear_app(Settings(), transport=httpx.MockTransport(_aprobado))
    with TestClient(app):
        caso = app.state.cobrar
    assert caso._proveedor._cliente.is_closed
    assert app.state.consultar_cobro._proveedor is caso._proveedor
