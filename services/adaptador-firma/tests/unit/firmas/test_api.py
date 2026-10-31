"""API POST /v1/firmas: cada correspondencia de errores, la configuración y los logs."""

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from adaptador_firma.config import Settings
from adaptador_firma.main import crear_app
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

DOCUMENTO = "5b1c9e2a-7f3d-4a60-8c11-000000000d01"
FIRMANTE = "00000000-0000-4000-8000-000000000001"
HUELLA = "sha256:" + "0123456789abcdef" * 4
CORRELACION = "corr-api-w17"

Manejador = Callable[[httpx.Request], httpx.Response | Exception]


def _eco(request: httpx.Request, **cambios: Any) -> httpx.Response:
    """El proveedor firma lo recibido: devuelve el documentoId y la huella enviados."""
    enviado = json.loads(request.content)
    cuerpo = {
        "documentoId": enviado["documentoId"],
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
        "huella": enviado["huella"],
    }
    cuerpo.update(cambios)
    return httpx.Response(200, json=cuerpo)


def _firmado(request: httpx.Request) -> httpx.Response:
    return _eco(request)


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
    cuerpo: dict[str, Any] = {"documentoId": DOCUMENTO, "huella": HUELLA, "firmanteRef": FIRMANTE}
    cuerpo.update(cambios)
    return {clave: valor for clave, valor in cuerpo.items() if valor is not _FALTA}


def _firmar(cliente: TestClient, **cambios: Any) -> httpx.Response:
    return cliente.post("/v1/firmas", json=_cuerpo(**cambios))


def _error(respuesta: httpx.Response, estado: int, codigo: str) -> dict[str, Any]:
    assert respuesta.status_code == estado, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == codigo
    assert cuerpo["correlationId"] == CORRELACION
    return cuerpo


# --- 200 -----------------------------------------------------------------------------------
def test_firmado(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_firmado)
    respuesta = _firmar(cliente)
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {
        "documentoId": DOCUMENTO,
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
        "huella": HUELLA,
    }
    (request,) = recibidas
    assert request.method == "POST"
    assert request.url.path == "/firma/v1/firmas"
    assert request.headers["X-Api-Key"] == "local-sintetico"
    assert request.headers["Idempotency-Key"] == DOCUMENTO
    assert request.headers["X-Correlation-Id"] == CORRELACION
    assert json.loads(request.content) == {
        "documentoId": DOCUMENTO,
        "huella": HUELLA,
        "firmante": FIRMANTE,
    }


def test_el_sello_sale_con_la_zona_del_proveedor(cliente_con: Any) -> None:
    cliente, _ = cliente_con(lambda r: _eco(r, selloTiempo="2030-06-15T08:30:00-05:00"))
    assert _firmar(cliente).json()["selloTiempo"] == "2030-06-15T08:30:00-05:00"


def test_la_idempotency_key_es_el_documento_de_cada_solicitud(cliente_con: Any) -> None:
    otro = "5b1c9e2a-7f3d-4a60-8c11-000000000d02"
    cliente, recibidas = cliente_con(_firmado)
    assert _firmar(cliente).status_code == 200
    assert _firmar(cliente, documentoId=otro).status_code == 200
    assert [r.headers["Idempotency-Key"] for r in recibidas] == [DOCUMENTO, otro]


# --- 422: entrada inválida, sin llamar al proveedor ------------------------------------------
@pytest.mark.parametrize(
    ("cambios", "campo"),
    [
        ({"documentoId": "no-uuid"}, "documentoId"),
        ({"documentoId": _FALTA}, "documentoId"),
        ({"huella": HUELLA.removeprefix("sha256:")}, "huella"),
        ({"huella": "md5:" + "a" * 32}, "huella"),
        ({"huella": HUELLA.upper()}, "huella"),
        ({"huella": "sha256:" + "A" * 64}, "huella"),
        ({"huella": HUELLA[:-1]}, "huella"),
        ({"huella": HUELLA + "0"}, "huella"),
        ({"huella": HUELLA + "\n"}, "huella"),
        ({"huella": " " + HUELLA}, "huella"),
        ({"huella": "sha256:" + "g" * 64}, "huella"),
        ({"huella": ""}, "huella"),
        ({"huella": 1}, "huella"),
        ({"huella": _FALTA}, "huella"),
        ({"firmanteRef": "no-uuid"}, "firmanteRef"),
        ({"firmanteRef": _FALTA}, "firmanteRef"),
    ],
)
def test_422_solicitud_invalida(cliente_con: Any, cambios: dict[str, Any], campo: str) -> None:
    cliente, recibidas = cliente_con(_firmado)
    cuerpo = _error(_firmar(cliente, **cambios), 422, "SOLICITUD_INVALIDA")
    assert cuerpo["detalles"]["campos"] == [campo]
    assert recibidas == []


def test_cuerpo_que_no_es_json_es_422(cliente_con: Any) -> None:
    """FastAPI responde 422 (`json_invalid`), aunque el contrato documenta el 400 del modelo."""
    cliente, recibidas = cliente_con(_firmado)
    respuesta = cliente.post(
        "/v1/firmas", content=b"{", headers={"Content-Type": "application/json"}
    )
    _error(respuesta, 422, "SOLICITUD_INVALIDA")
    assert recibidas == []


# --- Correspondencia de los fallos del proveedor ---------------------------------------------
def test_504_tiempo_agotado(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(lambda r: httpx.ReadTimeout("lento", request=r))
    _error(_firmar(cliente), 504, "TIEMPO_AGOTADO")
    assert len(recibidas) == 1


@pytest.mark.parametrize(
    "manejador",
    [
        pytest.param(lambda _: httpx.Response(503, json={"error": "unavailable"}), id="503"),
        pytest.param(lambda _: httpx.Response(500), id="500"),
        pytest.param(lambda r: httpx.ConnectError("caído", request=r), id="conexion"),
    ],
)
def test_503_fuente_no_disponible(cliente_con: Any, manejador: Manejador) -> None:
    cliente, recibidas = cliente_con(manejador)
    cuerpo = _error(_firmar(cliente), 503, "FUENTE_NO_DISPONIBLE")
    assert cuerpo["detalles"] == {"motivo": "ERROR", "circuito": "CERRADO"}
    # Sin reintentos dentro del adaptador.
    assert len(recibidas) == 1


@pytest.mark.parametrize(
    "manejador",
    [
        pytest.param(lambda _: httpx.Response(200, text='{"estado": '), id="malformado"),
        pytest.param(
            lambda _: httpx.Response(200, json={"estado": "FIRMADO"}), id="fuera-de-forma"
        ),
        pytest.param(
            lambda _: httpx.Response(404, json={"error": "documento_no_reconocido"}), id="404"
        ),
        pytest.param(lambda _: httpx.Response(400, json={"error": "x"}), id="400"),
        pytest.param(lambda _: httpx.Response(409), id="409"),
        pytest.param(lambda _: httpx.Response(302, headers={"Location": "/otra"}), id="302"),
        pytest.param(
            lambda r: _eco(r, documentoId="00000000-0000-4000-8000-0000000000ff"),
            id="otro-documento",
        ),
        pytest.param(lambda r: _eco(r, huella="sha256:" + "f" * 64), id="otra-huella"),
        pytest.param(lambda r: _eco(r, estado="RECHAZADO"), id="estado-rechazado"),
        pytest.param(lambda r: _eco(r, firmaId=""), id="firma-vacia"),
        pytest.param(lambda r: _eco(r, selloTiempo="2026-01-01T00:00:00"), id="sello-sin-zona"),
    ],
)
def test_502_respuesta_invalida(cliente_con: Any, manejador: Manejador) -> None:
    cliente, recibidas = cliente_con(manejador)
    cuerpo = _error(_firmar(cliente), 502, "RESPUESTA_INVALIDA")
    assert cuerpo["detalles"] == {}
    assert len(recibidas) == 1


# --- Timeout: ALLY_FIRMA_TIMEOUT_MS como corte duro (no los 700 ms por defecto) ---------------
@pytest.mark.parametrize(("variable", "esperado"), [(None, 5.0), ("1200", 1.2), ("8000", 8.0)])
def test_el_timeout_configurado_es_el_corte_duro(
    monkeypatch: pytest.MonkeyPatch, variable: str | None, esperado: float
) -> None:
    if variable is None:
        monkeypatch.delenv("ALLY_FIRMA_TIMEOUT_MS", raising=False)
    else:
        monkeypatch.setenv("ALLY_FIRMA_TIMEOUT_MS", variable)
    recibidas: list[httpx.Request] = []

    def manejador(request: httpx.Request) -> httpx.Response:
        recibidas.append(request)
        return _firmado(request)

    app = crear_app(Settings(), transport=httpx.MockTransport(manejador))
    with TestClient(app) as cliente:
        assert _firmar(cliente).status_code == 200
    assert recibidas[0].extensions["timeout"]["read"] == esperado


def test_el_corte_lo_aplica_la_proteccion_y_no_solo_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un proveedor que no respeta el timeout de httpx también se corta (asyncio.wait_for)."""
    monkeypatch.setenv("ALLY_FIRMA_TIMEOUT_MS", "100")

    async def colgado(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _firmado(request)

    app = crear_app(Settings(), transport=httpx.MockTransport(colgado))
    with TestClient(app) as cliente:
        assert _firmar(cliente).json()["codigo"] == "TIEMPO_AGOTADO"


# --- Logs: sin secreto, sin firmante y sin cuerpos -------------------------------------------
SECRETO = "secreto-centinela-firma"
FIRMANTE_CENTINELA = "00000000-0000-4000-8000-00000000c0de"
HUELLA_CENTINELA = "sha256:" + "c0de" * 16
SELLO_CENTINELA = "2031-07-17T11:22:33Z"


@pytest.mark.parametrize(
    "manejador",
    [
        pytest.param(lambda r: _eco(r, selloTiempo=SELLO_CENTINELA), id="200"),
        pytest.param(lambda r: httpx.ReadTimeout("lento", request=r), id="504"),
        pytest.param(lambda _: httpx.Response(503), id="503"),
        pytest.param(
            lambda _: httpx.Response(200, text=f'{{"huella": "{HUELLA_CENTINELA}"'), id="502"
        ),
        pytest.param(lambda r: _eco(r, huella="sha256:" + "f" * 64), id="502-otra-huella"),
    ],
)
def test_logs_sin_secreto_firmante_ni_cuerpos(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., Any], manejador: Manejador
) -> None:
    monkeypatch.setenv(
        "ALLY_FIRMA_CREDENTIALS",
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
        cliente.post(
            "/v1/firmas", json=_cuerpo(firmanteRef=FIRMANTE_CENTINELA, huella=HUELLA_CENTINELA)
        )
    assert recibidas[0].headers["X-Api-Key"] == SECRETO  # el secreto sí llega al proveedor
    texto = salida.getvalue()
    assert '"operation": "firma.documento"' in texto  # hay logs que revisar
    for centinela in (
        SECRETO,
        "id-centinela",
        FIRMANTE_CENTINELA,
        HUELLA_CENTINELA,
        "c0dec0de",
        SELLO_CENTINELA,
    ):
        assert centinela not in texto, centinela


# --- Configuración y ciclo de vida ----------------------------------------------------------
def test_configuracion_de_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los nombres que entrega infra/apps/ecs.tf para el aliado `firma`."""
    monkeypatch.setenv("ALLY_FIRMA_URL", "http://simulador-aliados:8080/firma")
    monkeypatch.setenv("ALLY_FIRMA_TIMEOUT_MS", "5000")
    monkeypatch.setenv(
        "ALLY_FIRMA_CREDENTIALS", json.dumps({"client_id": "id", "client_secret": "secreto"})
    )
    settings = Settings()
    assert settings.ally_firma_url == "http://simulador-aliados:8080/firma"
    assert settings.ally_firma_timeout_ms == 5000
    assert settings.ally_firma_credentials.client_secret.get_secret_value() == "secreto"
    assert settings.ally_firma_credentials.client_id.get_secret_value() == "id"


def test_valores_por_omision(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in ("ALLY_FIRMA_URL", "ALLY_FIRMA_TIMEOUT_MS", "ALLY_FIRMA_CREDENTIALS"):
        monkeypatch.delenv(nombre, raising=False)
    settings = Settings()
    # 5000 ms, el valor de `ally_timeouts_ms.firma` en infra/apps/variables.tf.
    assert settings.ally_firma_timeout_ms == 5000
    assert settings.ally_firma_url.endswith("/firma")


def test_credenciales_fuera_de_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ALLY_FIRMA_CREDENTIALS",
        json.dumps({"client_id": "id-centinela-firma", "client_secret": SECRETO}),
    )
    settings = Settings()
    for texto in (repr(settings), str(settings)):
        assert "id-centinela-firma" not in texto
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

    monkeypatch.setenv("ALLY_FIRMA_CREDENTIALS", credenciales)
    with pytest.raises((ValidationError, SettingsError)) as error:
        Settings()
    assert "solo-secreto" not in str(error.value)
    assert "solo-id" not in str(error.value)


def test_health_sin_dependencias(cliente_con: Any) -> None:
    cliente, recibidas = cliente_con(_firmado)
    assert cliente.get("/health").json() == {"status": "ok"}
    assert recibidas == []


def test_el_lifespan_cierra_el_cliente() -> None:
    app = crear_app(Settings(), transport=httpx.MockTransport(_firmado))
    with TestClient(app):
        caso = app.state.firmar
    assert caso._proveedor._cliente.is_closed
