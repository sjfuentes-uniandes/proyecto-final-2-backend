"""AdaptadorPagosHttp (HU-W16, 16.10): `POST /v1/cobros` de adaptador-pagos con httpx.MockTransport.

Solo un 200 con el `CobroDto` del contrato (`contracts/openapi/adaptador-pagos.yaml`) y la misma
`claveRecaudo` es una respuesta definitiva. Un timeout, un error de conexión, un 502, 503 o 504, un
422 inesperado, cualquier otro estado o un 200 fuera de contrato son inciertos: nunca un rechazo y
nunca una excepción (ADR 0013, decisión 5). Los timeouts, 5xx y cuerpos malformados van aquí, no en
el pacto.
"""

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from dinero.adapters.outbound import pagos_http
from dinero.adapters.outbound.pagos_http import AdaptadorPagosHttp
from dinero.application.recaudo.puertos import (
    CobroAprobado,
    CobroIncierto,
    CobroRechazado,
    SolicitudCobro,
)
from solventa_common import correlation

pytestmark = pytest.mark.anyio
PAGADOR = UUID("00000000-0000-4000-8000-000000000001")
BASE = "http://adaptador-pagos:8080"
Responder = Callable[[httpx.Request], httpx.Response]


def solicitud(**cambios: Any) -> SolicitudCobro:
    datos: dict[str, Any] = {
        "clave_recaudo": uuid4(),
        "monto": Decimal("64638.00"),
        "moneda": "COP",
        "pagador_ref": PAGADOR,
        "correlation_id": "corr-w16-http",
    }
    datos.update(cambios)
    return SolicitudCobro(**datos)


class Registro:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.solicitudes: list[httpx.Request] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.solicitudes.append(request)
        self.timeouts.append(request.extensions.get("timeout"))
        return self.responder(request)


async def cobrar(
    responder: Responder | Any, entrada: SolicitudCobro | None = None, timeout_ms: int = 6500
) -> tuple[Any, Registro]:
    registro = Registro(responder) if callable(responder) else responder
    entrada = entrada or solicitud()
    async with correlation.cliente_async(transport=httpx.MockTransport(registro)) as cliente:
        resultado = await AdaptadorPagosHttp(cliente, BASE + "/", timeout_ms).cobrar(entrada)
    return resultado, registro


def cobro(clave: Any, estado: str, transaccion: Any, motivo: Any = None) -> dict[str, Any]:
    return {
        "claveRecaudo": str(clave),
        "estado": estado,
        "transaccionId": transaccion,
        "motivo": motivo,
    }


def eco(estado: str, transaccion: Any, motivo: Any = None) -> Responder:
    def responder(request: httpx.Request) -> httpx.Response:
        clave = json.loads(request.content)["claveRecaudo"]
        return httpx.Response(200, json=cobro(clave, estado, transaccion, motivo))

    return responder


# --- La solicitud ---------------------------------------------------------------------------------
async def test_la_solicitud_sigue_el_contrato_con_el_monto_como_texto() -> None:
    entrada = solicitud(monto=Decimal("64638.07"))
    _, registro = await cobrar(eco("APROBADO", "TX-SINT-C01"), entrada)
    [request] = registro.solicitudes
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/v1/cobros")
    cuerpo = json.loads(request.content)
    assert cuerpo == {
        "claveRecaudo": str(entrada.clave_recaudo),
        "monto": "64638.07",
        "moneda": "COP",
        "pagadorRef": str(PAGADOR),
    }
    assert isinstance(cuerpo["monto"], str)  # nunca un número JSON (422 en el adaptador)
    assert request.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("monto", "texto"),
    [
        (Decimal(64638), "64638"),
        (Decimal("64638.00"), "64638.00"),
        (Decimal("0.10"), "0.10"),
        (Decimal("1E+3"), "1000"),  # sin notación científica: el adaptador exige ^[0-9]+...
    ],
)
async def test_el_monto_va_en_texto_decimal_exacto(monto: Decimal, texto: str) -> None:
    _, registro = await cobrar(eco("APROBADO", "TX"), solicitud(monto=monto))
    assert json.loads(registro.solicitudes[0].content)["monto"] == texto


async def test_propaga_el_correlation_id() -> None:
    _, registro = await cobrar(eco("APROBADO", "TX"), solicitud(correlation_id="corr-propagada"))
    assert registro.solicitudes[0].headers["X-Correlation-Id"] == "corr-propagada"


async def test_el_contexto_de_correlacion_no_reemplaza_al_del_movimiento() -> None:
    token = correlation.establecer("corr-del-mensaje")
    try:
        _, registro = await cobrar(
            eco("APROBADO", "TX"), solicitud(correlation_id="corr-del-mensaje")
        )
    finally:
        correlation.restablecer(token)
    assert registro.solicitudes[0].headers["X-Correlation-Id"] == "corr-del-mensaje"


async def test_el_timeout_de_la_llamada_es_el_configurado() -> None:
    _, registro = await cobrar(eco("APROBADO", "TX"), timeout_ms=6500)
    [timeout] = registro.timeouts
    assert timeout == {"connect": 6.5, "read": 6.5, "write": 6.5, "pool": 6.5}


# --- Respuestas definitivas -----------------------------------------------------------------------
async def test_aprobado() -> None:
    resultado, _ = await cobrar(eco("APROBADO", "TX-SINT-C01"))
    assert resultado == CobroAprobado("TX-SINT-C01")


@pytest.mark.parametrize(
    ("transaccion", "motivo"),
    [(None, "FONDOS_INSUFICIENTES"), ("TX-R-01", None), (None, "texto libre del proveedor")],
)
async def test_rechazado_tal_como_lo_informa_el_adaptador(transaccion: Any, motivo: Any) -> None:
    """La normalización del motivo a un código es del dominio (`motivo_de_rechazo`)."""
    resultado, _ = await cobrar(eco("RECHAZADO", transaccion, motivo))
    assert resultado == CobroRechazado(motivo=motivo, transaccion_id=transaccion)


# --- Inciertos: nunca un rechazo ni una excepción -------------------------------------------------
@pytest.mark.parametrize(
    ("estado", "codigo"),
    [
        (504, "TIEMPO_AGOTADO"),
        (503, "FUENTE_NO_DISPONIBLE"),
        (502, "RESPUESTA_INVALIDA"),
        (422, "SOLICITUD_INVALIDA"),
        (400, "ERROR_HTTP"),
        (404, "NO_ENCONTRADO"),
        (500, "ERROR_INTERNO"),
    ],
)
async def test_un_error_del_adaptador_es_incierto_con_su_codigo(estado: int, codigo: str) -> None:
    def responder(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            estado,
            json={"codigo": codigo, "mensaje": "x", "correlationId": "c", "detalles": {}},
        )

    resultado, _ = await cobrar(responder)
    assert resultado == CobroIncierto(codigo)


@pytest.mark.parametrize(
    ("estado", "cuerpo"),
    [
        (503, "Service Unavailable"),
        (502, '{"codigo": "texto libre con Juan Pérez"}'),
        (500, '{"codigo": 42}'),
        (504, "[]"),
        (301, ""),
        (201, '{"estado": "APROBADO"}'),
    ],
)
async def test_un_estado_sin_codigo_reconocible_es_incierto_http(estado: int, cuerpo: str) -> None:
    resultado, _ = await cobrar(lambda _: httpx.Response(estado, content=cuerpo.encode()))
    assert resultado == CobroIncierto(f"HTTP_{estado}")


async def test_un_timeout_es_incierto_por_tiempo_agotado() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    resultado, _ = await cobrar(responder)
    assert resultado == CobroIncierto(pagos_http.TIEMPO_AGOTADO)
    assert pagos_http.TIEMPO_AGOTADO == "ADAPTADOR_TIEMPO_AGOTADO"


async def test_una_respuesta_que_no_termina_dentro_del_corte_total_es_incierto() -> None:
    """El timeout de httpx es por fase; además hay un corte total de la llamada."""

    class Lento(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            raise AssertionError("no debió esperar tanto")

    async with httpx.AsyncClient(transport=Lento()) as cliente:
        inicio = asyncio.get_running_loop().time()
        resultado = await AdaptadorPagosHttp(cliente, BASE, 50).cobrar(solicitud())
    assert resultado == CobroIncierto(pagos_http.TIEMPO_AGOTADO)
    assert asyncio.get_running_loop().time() - inicio < 2


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError])
async def test_un_error_de_conexion_es_incierto_por_no_disponible(error: type[Exception]) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise error("sin conexión", request=request)  # type: ignore[call-arg]

    resultado, _ = await cobrar(responder)
    assert resultado == CobroIncierto(pagos_http.NO_DISPONIBLE)
    assert pagos_http.NO_DISPONIBLE == "ADAPTADOR_NO_DISPONIBLE"


def _con_clave(cambio: Callable[[dict[str, Any]], Any]) -> Responder:
    def responder(request: httpx.Request) -> httpx.Response:
        cuerpo = cobro(json.loads(request.content)["claveRecaudo"], "APROBADO", "TX-SINT-C01")
        resultado = cambio(cuerpo)
        contenido = resultado if isinstance(resultado, bytes) else json.dumps(resultado).encode()
        return httpx.Response(200, content=contenido)

    return responder


@pytest.mark.parametrize(
    "cambio",
    [
        lambda c: b'{"estado": ',  # malformado
        lambda c: [c],
        lambda c: {**c, "claveRecaudo": str(uuid4())},  # el cobro de otra clave
        lambda c: {**c, "claveRecaudo": "no-es-uuid"},
        lambda c: {**c, "estado": "PENDIENTE"},
        lambda c: {**c, "estado": "aprobado"},
        lambda c: {**c, "transaccionId": None},  # APROBADO sin transacción
        lambda c: {**c, "transaccionId": ""},
        lambda c: {**c, "transaccionId": "   "},
        lambda c: {**c, "transaccionId": "T" * 129},
        lambda c: {**c, "transaccionId": 12345},
        lambda c: {k: v for k, v in c.items() if k != "estado"},
        lambda c: {**c, "estado": "RECHAZADO", "transaccionId": "T" * 129},
        lambda c: {**c, "estado": "RECHAZADO", "transaccionId": None, "motivo": 7},
    ],
    ids=[
        "malformado",
        "lista",
        "otra-clave",
        "clave-no-uuid",
        "estado-pendiente",
        "estado-en-minuscula",
        "aprobado-sin-transaccion",
        "transaccion-vacia",
        "transaccion-en-blanco",
        "transaccion-larga",
        "transaccion-numero",
        "sin-estado",
        "rechazado-transaccion-larga",
        "rechazado-motivo-numero",
    ],
)
async def test_un_200_fuera_de_contrato_es_incierto(
    cambio: Callable[[dict[str, Any]], Any],
) -> None:
    resultado, _ = await cobrar(_con_clave(cambio))
    assert resultado == CobroIncierto(pagos_http.RESPUESTA_INVALIDA)
    assert pagos_http.RESPUESTA_INVALIDA == "ADAPTADOR_RESPUESTA_INVALIDA"


async def test_no_registra_el_pagador_ni_los_cuerpos(logs: Any) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"Juan Perez, cuenta 4111111111111111")

    await cobrar(responder)
    await cobrar(eco("APROBADO", "TX-SINT-C01"))
    texto = logs.texto()
    assert str(PAGADOR) not in texto
    assert "Juan" not in texto and "4111" not in texto
