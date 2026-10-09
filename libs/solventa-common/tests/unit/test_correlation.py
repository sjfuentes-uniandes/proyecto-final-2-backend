import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from solventa_common import correlation


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/eco")
    async def eco() -> dict[str, str | None]:
        return {"correlation_id": correlation.obtener()}

    app.add_middleware(correlation.CorrelacionMiddleware)
    return app


@pytest.mark.parametrize("valor", ["prueba-123", "a", "A.b_c:d-9", "x" * 128, str(uuid.uuid4())])
def test_ids_validos(valor: str) -> None:
    assert correlation.es_valido(valor)


@pytest.mark.parametrize(
    "valor",
    [None, "", "x" * 129, "con espacio", "inyección\n", "<script>", "a/b", "id;drop", "ñandú", "tab\t"],
)
def test_ids_invalidos(valor: str | None) -> None:
    assert not correlation.es_valido(valor)


def test_prioridad_correlation_request_uuid() -> None:
    assert correlation.resolver("socio-1", "req-1") == "socio-1"
    assert correlation.resolver(None, "req-1") == "req-1"
    assert correlation.resolver("no válido", "req-1") == "req-1"
    generado = correlation.resolver(None, None)
    assert uuid.UUID(generado).version == 4
    assert uuid.UUID(correlation.resolver("x" * 200, "también inválido")).version == 4


def test_genera_id_si_no_llega_y_lo_devuelve() -> None:
    respuesta = TestClient(_app()).get("/eco")
    generado = respuesta.headers["X-Correlation-Id"]
    assert uuid.UUID(generado).version == 4
    assert respuesta.json() == {"correlation_id": generado}


def test_respeta_el_id_del_socio() -> None:
    respuesta = TestClient(_app()).get("/eco", headers={"X-Correlation-Id": "prueba-123"})
    assert respuesta.headers["X-Correlation-Id"] == "prueba-123"
    assert respuesta.json() == {"correlation_id": "prueba-123"}


def test_usa_x_request_id_de_api_gateway() -> None:
    respuesta = TestClient(_app()).get("/eco", headers={"X-Request-Id": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"})
    assert respuesta.headers["X-Correlation-Id"] == "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"


def test_rechaza_id_invalido_y_usa_el_siguiente() -> None:
    cliente = TestClient(_app())
    respuesta = cliente.get("/eco", headers={"X-Correlation-Id": "x" * 129, "X-Request-Id": "req-9"})
    assert respuesta.headers["X-Correlation-Id"] == "req-9"
    respuesta = cliente.get("/eco", headers={"X-Correlation-Id": "<script>alert(1)</script>"})
    assert "<" not in respuesta.headers["X-Correlation-Id"]
    assert uuid.UUID(respuesta.headers["X-Correlation-Id"]).version == 4


def test_no_duplica_el_encabezado_si_la_ruta_ya_lo_puso() -> None:
    app = _app()

    @app.get("/propio")
    async def propio() -> dict[str, str]:
        from fastapi.responses import JSONResponse

        return JSONResponse({}, headers={"X-Correlation-Id": "otro"})  # type: ignore[return-value]

    respuesta = TestClient(app).get("/propio", headers={"X-Correlation-Id": "prueba-123"})
    assert respuesta.headers.get_list("X-Correlation-Id") == ["prueba-123"]


def test_encabezados_configurables() -> None:
    app = FastAPI()

    @app.get("/eco")
    async def eco() -> dict[str, str | None]:
        return {"correlation_id": correlation.obtener()}

    app.add_middleware(correlation.CorrelacionMiddleware, correlation_header="X-Trace-Ref", request_id_header="X-Req")
    respuesta = TestClient(app).get("/eco", headers={"X-Req": "req-7"})
    assert respuesta.headers["X-Trace-Ref"] == "req-7"


def test_el_contexto_se_limpia_al_terminar() -> None:
    TestClient(_app()).get("/eco", headers={"X-Correlation-Id": "temporal"})
    assert correlation.obtener() is None


def test_hook_httpx_propaga_id_y_contexto_de_traza() -> None:
    recibidos: list[httpx.Request] = []
    transporte = httpx.MockTransport(lambda request: recibidos.append(request) or httpx.Response(200))
    token = correlation.establecer("prueba-123")
    try:
        with correlation.cliente(transport=transporte) as cliente:
            cliente.get("http://otro-servicio/recurso")
    finally:
        correlation.restablecer(token)
    assert recibidos[0].headers["X-Correlation-Id"] == "prueba-123"


def test_hook_httpx_async_propaga_id() -> None:
    recibidos: list[httpx.Request] = []
    transporte = httpx.MockTransport(lambda request: recibidos.append(request) or httpx.Response(200))

    async def llamar() -> None:
        token = correlation.establecer("async-1")
        try:
            async with correlation.cliente_async(transport=transporte) as cliente:
                await cliente.get("http://otro-servicio/recurso")
        finally:
            correlation.restablecer(token)

    asyncio.run(llamar())
    assert recibidos[0].headers["X-Correlation-Id"] == "async-1"


def test_hook_conserva_otros_hooks_y_no_agrega_nada_sin_contexto() -> None:
    llamados: list[str] = []
    recibidos: list[httpx.Request] = []
    transporte = httpx.MockTransport(lambda request: recibidos.append(request) or httpx.Response(200))
    with correlation.cliente(transport=transporte, event_hooks={"request": [lambda r: llamados.append("propio")]}) as c:
        c.get("http://otro-servicio/")
    assert llamados == ["propio"]
    assert "X-Correlation-Id" not in recibidos[0].headers


def test_encabezados_salientes_para_otros_clientes() -> None:
    token = correlation.establecer("cola-1")
    try:
        assert correlation.encabezados_salientes({"Accept": "application/json"})["X-Correlation-Id"] == "cola-1"
    finally:
        correlation.restablecer(token)
