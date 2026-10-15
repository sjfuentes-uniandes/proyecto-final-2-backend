"""GET /v1/sesion: servicios autorizados, fallas de `clientes`, correlación y observabilidad."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest

Entorno = Any
CORRELACION = "corr-m07-sesion"
FORMATO_FECHA = "%Y-%m-%dT%H:%M:%SZ"


def _expira_en(token: str) -> str:
    exp = jwt.decode(token, options={"verify_signature": False})["exp"]
    return datetime.fromtimestamp(exp, UTC).strftime(FORMATO_FECHA)


@pytest.mark.hu("M07", "AC1")
def test_cliente_verificado_recibe_polizas_siniestros_y_privacidad(entorno: Entorno) -> None:
    token = entorno.emitir()

    respuesta = entorno.consultar(token, encabezados={"X-Correlation-Id": CORRELACION})

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "clienteId": str(entorno.cliente_id),
        "serviciosAutorizados": ["POLIZAS", "SINIESTROS", "PRIVACIDAD"],
        "expiraEn": _expira_en(token),
    }
    solicitud = entorno.identidad.calls.last.request
    assert solicitud.headers["X-Actor-Id"] == entorno.sub
    assert solicitud.headers["X-Actor-Rol"] == "CLIENTE"
    assert entorno.identidad.call_count == 1


@pytest.mark.hu("M07", "AC1")
@pytest.mark.parametrize("estado", ["PENDIENTE", "RECHAZADO"])
def test_cliente_sin_identidad_verificada_solo_ve_la_verificacion(
    entorno: Entorno, estado: str
) -> None:
    entorno.identidad_responde(estado)

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 200
    assert respuesta.json()["serviciosAutorizados"] == ["VERIFICACION_IDENTIDAD"]


def test_devuelve_y_propaga_el_mismo_correlation_id(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir(), encabezados={"X-Correlation-Id": CORRELACION})

    assert respuesta.headers["X-Correlation-Id"] == CORRELACION
    assert entorno.identidad.calls.last.request.headers["X-Correlation-Id"] == CORRELACION


def test_el_error_conserva_el_correlation_id_recibido(entorno: Entorno) -> None:
    respuesta = entorno.consultar(encabezados={"X-Correlation-Id": CORRELACION})

    assert respuesta.status_code == 401
    assert respuesta.headers["X-Correlation-Id"] == CORRELACION
    assert respuesta.json()["correlationId"] == CORRELACION


def test_cliente_inexistente_en_clientes_responde_404(entorno: Entorno) -> None:
    entorno.identidad.mock(return_value=httpx.Response(404))

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "CLIENTE_NO_ENCONTRADO"


def test_titular_sin_cliente_asociado_responde_404_sin_llamar_a_clientes(
    crear_entorno: Callable[..., Entorno],
) -> None:
    entorno = crear_entorno(con_cliente=False)

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 404
    assert respuesta.json()["codigo"] == "CLIENTE_NO_ENCONTRADO"
    assert entorno.identidad.call_count == 0


def test_timeout_de_clientes_responde_503_con_una_sola_llamada(entorno: Entorno) -> None:
    entorno.identidad.mock(side_effect=httpx.ReadTimeout("sin respuesta"))

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert entorno.identidad.call_count == 1
    cierre = [
        log
        for log in entorno.telemetria.logs()
        if log.get("operation") == "sesion.consultar_identidad"
    ]
    assert cierre[-1]["result"] == "timeout"


def test_clientes_que_excede_el_timeout_configurado_responde_503_sin_reintentar(
    crear_entorno: Callable[..., Entorno],
) -> None:
    entorno = crear_entorno(clientes_timeout_ms=50)
    llamadas = 0

    async def lento(_: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        await asyncio.sleep(1)
        return httpx.Response(200)

    entorno.identidad.mock(side_effect=lento)

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert llamadas == 1


def test_error_500_de_clientes_responde_503(entorno: Entorno) -> None:
    entorno.identidad.mock(return_value=httpx.Response(500))

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert entorno.identidad.call_count == 1


def test_error_de_conexion_con_clientes_responde_503(entorno: Entorno) -> None:
    entorno.identidad.mock(side_effect=httpx.ConnectError("sin ruta"))

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"


@pytest.mark.parametrize(
    "respuesta_clientes",
    [
        httpx.Response(400),
        httpx.Response(403),
        httpx.Response(200, content=b"no es json"),
        httpx.Response(200, json={"clienteId": "00000000-0000-4000-8000-000000000001"}),
        httpx.Response(200, json={"clienteId": "x", "estado": "VERIFICADO"}),
        httpx.Response(
            200, json={"clienteId": "00000000-0000-4000-8000-000000000002", "estado": "VERIFICADO"}
        ),
        httpx.Response(
            200, json={"clienteId": "00000000-0000-4000-8000-000000000001", "estado": "OTRO"}
        ),
    ],
    ids=[
        "400",
        "403",
        "no-json",
        "sin-estado",
        "id-invalido",
        "otro-cliente",
        "estado-desconocido",
    ],
)
def test_respuesta_de_clientes_fuera_de_contrato_falla_cerrado(
    entorno: Entorno, respuesta_clientes: httpx.Response
) -> None:
    entorno.identidad.mock(return_value=respuesta_clientes)

    respuesta = entorno.consultar(entorno.emitir())

    assert respuesta.status_code == 503
    assert respuesta.json()["codigo"] == "SERVICIO_NO_DISPONIBLE"


def _sin_token(entorno: Entorno) -> httpx.Response:
    return entorno.consultar()


def _token_vencido(entorno: Entorno) -> httpx.Response:
    return entorno.consultar(entorno.emitir(vigencia=timedelta(minutes=-5)))


def _otro_pool(entorno: Entorno) -> httpx.Response:
    return entorno.consultar(entorno.emitir(emisor=entorno.emisor_backoffice))


def _cliente_inexistente(entorno: Entorno) -> httpx.Response:
    entorno.identidad.mock(return_value=httpx.Response(404))
    return entorno.consultar(entorno.emitir())


def _clientes_caido(entorno: Entorno) -> httpx.Response:
    entorno.identidad.mock(return_value=httpx.Response(500))
    return entorno.consultar(entorno.emitir())


def _ruta_inexistente(entorno: Entorno) -> httpx.Response:
    return entorno.cliente.get("/v1/no-existe")


@pytest.mark.parametrize(
    ("provocar", "estado", "codigo"),
    [
        (_sin_token, 401, "NO_AUTENTICADO"),
        (_token_vencido, 401, "NO_AUTENTICADO"),
        (_otro_pool, 403, "ROL_NO_AUTORIZADO"),
        (_cliente_inexistente, 404, "CLIENTE_NO_ENCONTRADO"),
        (_clientes_caido, 503, "SERVICIO_NO_DISPONIBLE"),
        (_ruta_inexistente, 404, "NO_ENCONTRADO"),
    ],
)
def test_todos_los_errores_tienen_el_formato_estable(
    entorno: Entorno, provocar: Callable[[Entorno], httpx.Response], estado: int, codigo: str
) -> None:
    respuesta = provocar(entorno)

    cuerpo: dict[str, Any] = respuesta.json()
    assert respuesta.status_code == estado
    assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
    assert cuerpo["codigo"] == codigo
    assert isinstance(cuerpo["mensaje"], str)
    assert cuerpo["correlationId"] == respuesta.headers["X-Correlation-Id"]
    assert cuerpo["detalles"] == {}


def test_mide_la_solicitud_y_la_consulta_de_identidad(entorno: Entorno) -> None:
    entorno.consultar(entorno.emitir())

    telemetria = entorno.telemetria
    assert telemetria.puntos("OperationDuration", "GET /v1/sesion")[0].count == 1
    assert telemetria.puntos("OperationDuration", "sesion.consultar_identidad")[0].count == 1
    assert telemetria.puntos("OperationErrors", "sesion.consultar_identidad")[0].value == 0


def test_no_registra_el_token_en_logs_ni_trazas(entorno: Entorno) -> None:
    valido = entorno.emitir()
    vencido = entorno.emitir(vigencia=timedelta(minutes=-5))

    entorno.consultar(valido)
    entorno.consultar(vencido)

    telemetria = entorno.telemetria
    assert telemetria.logs()
    for centinela in (valido, vencido, valido.split(".")[2], vencido.split(".")[2]):
        assert centinela not in telemetria.texto_logs()
        assert centinela not in telemetria.texto_spans()
    rechazos = [log for log in telemetria.logs() if log["message"] == "sesión rechazada"]
    assert [log["motivo"] for log in rechazos] == ["expirado"]


def test_la_respuesta_no_expone_datos_de_otro_cliente(entorno: Entorno) -> None:
    respuesta = entorno.consultar(entorno.emitir())

    assert UUID(respuesta.json()["clienteId"]) == entorno.cliente_id
    assert set(respuesta.json()) == {"clienteId", "serviciosAutorizados", "expiraEn"}
