"""AdaptadorPagosHttp (HU-W23): `POST /v1/desembolsos` y `GET /v1/desembolsos/{referencia}` de
adaptador-pagos con httpx.MockTransport.

Solo un 200 con el `DesembolsoDto` del contrato (`contracts/openapi/adaptador-pagos.yaml`) y la
misma `referencia` es una respuesta definitiva. Un timeout, un error de conexión, un 502, 503 o 504,
un 422 inesperado, cualquier otro estado o un 200 fuera de contrato son inciertos: nunca un rechazo
y nunca una excepción (ADR 0013, decisión 5). El `beneficiarioRef` nunca se registra.
"""

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from dinero.adapters.outbound import pagos_http
from dinero.adapters.outbound.pagos_http import AdaptadorPagosHttp
from dinero.application.indemnizacion.puertos import (
    ConsultaDesembolso,
    DesembolsoConfirmado,
    DesembolsoIncierto,
    DesembolsoNoEncontrado,
    DesembolsoRechazado,
    SolicitudDesembolso,
)
from solventa_common import correlation

pytestmark = pytest.mark.anyio
BASE = "http://adaptador-pagos:8080"
REFERENCIA = "IND-88-2026-0442-01"
Responder = Callable[[httpx.Request], httpx.Response]


def solicitud(**cambios: Any) -> SolicitudDesembolso:
    datos: dict[str, Any] = {
        "referencia": REFERENCIA,
        "monto": Decimal("2500000.00"),
        "moneda": "COP",
        "beneficiario_ref": "ben-sint-001",
        "correlation_id": "corr-w23-http",
    }
    datos.update(cambios)
    return SolicitudDesembolso(**datos)


def consulta(**cambios: Any) -> ConsultaDesembolso:
    datos: dict[str, Any] = {
        "referencia": REFERENCIA,
        "beneficiario_ref": "ben-sint-001",
        "correlation_id": "corr-w23-http",
    }
    datos.update(cambios)
    return ConsultaDesembolso(**datos)


class Registro:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.solicitudes: list[httpx.Request] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.solicitudes.append(request)
        self.timeouts.append(request.extensions.get("timeout"))
        return self.responder(request)


async def desembolsar(
    responder: Responder | Any, entrada: SolicitudDesembolso | None = None, timeout_ms: int = 6500
) -> tuple[Any, Registro]:
    registro = Registro(responder) if callable(responder) else responder
    async with correlation.cliente_async(transport=httpx.MockTransport(registro)) as cliente:
        resultado = await AdaptadorPagosHttp(cliente, BASE + "/", timeout_ms).desembolsar(
            entrada or solicitud()
        )
    return resultado, registro


async def consultar(
    responder: Responder | Any, entrada: ConsultaDesembolso | None = None, timeout_ms: int = 6500
) -> tuple[Any, Registro]:
    registro = Registro(responder) if callable(responder) else responder
    async with correlation.cliente_async(transport=httpx.MockTransport(registro)) as cliente:
        resultado = await AdaptadorPagosHttp(cliente, BASE, timeout_ms).consultar_desembolso(
            entrada or consulta()
        )
    return resultado, registro


def dto(estado: str, transaccion: Any, motivo: Any = None, referencia: str = REFERENCIA) -> Any:
    return {
        "referencia": referencia,
        "estado": estado,
        "transaccionId": transaccion,
        "motivo": motivo,
    }


def devuelve(cuerpo: Any, estado: int = 200) -> Responder:
    return lambda _: httpx.Response(estado, json=cuerpo)


# --- La solicitud ---------------------------------------------------------------------------------
async def test_la_solicitud_sigue_el_contrato_con_el_monto_como_texto() -> None:
    _, registro = await desembolsar(devuelve(dto("CONFIRMADO", "TRX-1")))
    [request] = registro.solicitudes
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/v1/desembolsos")
    cuerpo = json.loads(request.content)
    assert cuerpo == {
        "referencia": REFERENCIA,
        "monto": "2500000.00",
        "moneda": "COP",
        "beneficiarioRef": "ben-sint-001",
    }
    assert isinstance(cuerpo["monto"], str)
    assert request.headers["X-Correlation-Id"] == "corr-w23-http"


@pytest.mark.parametrize(
    ("monto", "texto"),
    [
        (Decimal(2500000), "2500000"),
        (Decimal("2500000.50"), "2500000.50"),
        (Decimal("1E+3"), "1000"),  # sin notación científica
    ],
)
async def test_el_monto_va_en_texto_decimal_exacto(monto: Decimal, texto: str) -> None:
    _, registro = await desembolsar(devuelve(dto("CONFIRMADO", "T")), solicitud(monto=monto))
    assert json.loads(registro.solicitudes[0].content)["monto"] == texto


async def test_el_timeout_de_la_llamada_es_el_configurado() -> None:
    _, registro = await desembolsar(devuelve(dto("CONFIRMADO", "T")), timeout_ms=6500)
    assert registro.timeouts == [{"connect": 6.5, "read": 6.5, "write": 6.5, "pool": 6.5}]


async def test_la_consulta_va_a_la_ruta_con_el_beneficiario_y_la_correlacion() -> None:
    _, registro = await consultar(devuelve(dto("CONFIRMADO", "TRX-1")))
    [request] = registro.solicitudes
    assert (request.method, request.url.path) == ("GET", f"/v1/desembolsos/{REFERENCIA}")
    assert dict(request.url.params) == {"beneficiarioRef": "ben-sint-001"}
    assert request.headers["X-Correlation-Id"] == "corr-w23-http"
    assert request.content == b""


# --- Definitivos ----------------------------------------------------------------------------------
@pytest.mark.parametrize("llamar", [desembolsar, consultar])
async def test_confirmado_con_su_transaccion(llamar: Any) -> None:
    resultado, _ = await llamar(devuelve(dto("CONFIRMADO", "TRX-1")))
    assert resultado == DesembolsoConfirmado("TRX-1")


@pytest.mark.parametrize("llamar", [desembolsar, consultar])
async def test_rechazado_con_su_motivo(llamar: Any) -> None:
    resultado, _ = await llamar(devuelve(dto("RECHAZADO", None, "BENEFICIARIO_NO_HABILITADO")))
    assert resultado == DesembolsoRechazado(
        motivo="BENEFICIARIO_NO_HABILITADO", transaccion_id=None
    )


async def test_rechazado_puede_traer_la_transaccion_y_no_traer_motivo() -> None:
    resultado, _ = await desembolsar(devuelve(dto("RECHAZADO", "TRX-R", None)))
    assert resultado == DesembolsoRechazado(motivo=None, transaccion_id="TRX-R")


async def test_la_consulta_404_con_su_codigo_es_no_encontrado() -> None:
    resultado, _ = await consultar(devuelve({"codigo": "DESEMBOLSO_NO_ENCONTRADO"}, 404))
    assert resultado == DesembolsoNoEncontrado()


async def test_un_404_sin_ese_codigo_es_incierto() -> None:
    resultado, _ = await consultar(devuelve({"codigo": "RUTA_INEXISTENTE"}, 404))
    assert resultado == DesembolsoIncierto("RUTA_INEXISTENTE")


async def test_un_404_en_el_desembolso_no_es_no_encontrado() -> None:
    resultado, _ = await desembolsar(devuelve({"codigo": "DESEMBOLSO_NO_ENCONTRADO"}, 404))
    assert resultado == DesembolsoIncierto("DESEMBOLSO_NO_ENCONTRADO")


# --- Inciertos: nunca un rechazo ni una excepción --------------------------------------------------
@pytest.mark.parametrize("llamar", [desembolsar, consultar])
@pytest.mark.parametrize(
    ("estado", "cuerpo", "codigo"),
    [
        (504, {"codigo": "TIEMPO_AGOTADO"}, "TIEMPO_AGOTADO"),
        (503, {"codigo": "FUENTE_NO_DISPONIBLE"}, "FUENTE_NO_DISPONIBLE"),
        (502, {"codigo": "RESPUESTA_INVALIDA"}, "RESPUESTA_INVALIDA"),
        (422, {"codigo": "SOLICITUD_INVALIDA"}, "SOLICITUD_INVALIDA"),
        (500, {"mensaje": "sin código"}, "HTTP_500"),
        (302, {}, "HTTP_302"),
        (409, "texto", "HTTP_409"),
    ],
)
async def test_otros_estados_son_inciertos_con_su_codigo(
    llamar: Any, estado: int, cuerpo: Any, codigo: str
) -> None:
    resultado, _ = await llamar(devuelve(cuerpo, estado))
    assert resultado == DesembolsoIncierto(codigo)


@pytest.mark.parametrize("llamar", [desembolsar, consultar])
@pytest.mark.parametrize(
    ("error", "codigo"),
    [
        (httpx.ReadTimeout("lento"), pagos_http.TIEMPO_AGOTADO),
        (httpx.ConnectTimeout("lento"), pagos_http.TIEMPO_AGOTADO),
        (httpx.ConnectError("caído"), pagos_http.NO_DISPONIBLE),
    ],
)
async def test_un_fallo_de_transporte_es_incierto(
    llamar: Any, error: Exception, codigo: str
) -> None:
    def falla(request: httpx.Request) -> httpx.Response:
        raise error

    resultado, _ = await llamar(falla)
    assert resultado == DesembolsoIncierto(codigo)


@pytest.mark.parametrize("llamar", [desembolsar, consultar])
async def test_un_proveedor_que_no_responde_a_tiempo_es_incierto(llamar: Any) -> None:
    async def colgado(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    resultado, _ = await llamar(colgado, timeout_ms=50)
    assert resultado == DesembolsoIncierto(pagos_http.TIEMPO_AGOTADO)


@pytest.mark.parametrize("llamar", [desembolsar, consultar])
@pytest.mark.parametrize(
    "cuerpo",
    [
        dto("CONFIRMADO", None),
        dto("CONFIRMADO", ""),
        dto("CONFIRMADO", "   "),
        dto("CONFIRMADO", "x" * 129),
        dto("CONFIRMADO", 5),
        dto("APROBADO", "TRX"),
        dto("PENDIENTE", None),
        dto("CONFIRMADO", "TRX", referencia="IND-88-2026-9999-01"),
        {"estado": "CONFIRMADO"},
        {"referencia": REFERENCIA, "estado": "CONFIRMADO", "transaccionId": "T"},
        [1, 2],
        "texto",
    ],
)
async def test_un_200_fuera_de_contrato_es_incierto(llamar: Any, cuerpo: Any) -> None:
    resultado, _ = await llamar(devuelve(cuerpo))
    assert resultado == DesembolsoIncierto(pagos_http.RESPUESTA_INVALIDA)


@pytest.mark.parametrize("llamar", [desembolsar, consultar])
async def test_un_200_que_no_es_json_es_incierto(llamar: Any) -> None:
    resultado, _ = await llamar(lambda _: httpx.Response(200, text='{"estado": '))
    assert resultado == DesembolsoIncierto(pagos_http.RESPUESTA_INVALIDA)


async def test_confirmado_sin_transaccion_en_la_forma_del_dto_es_incierto() -> None:
    # `transaccionId` null en CONFIRMADO: el DTO valida y el dominio lo rechazaría; es incierto.
    resultado, _ = await desembolsar(devuelve(dto("CONFIRMADO", None)))
    assert resultado == DesembolsoIncierto(pagos_http.RESPUESTA_INVALIDA)
