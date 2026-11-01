"""AdaptadorFirmaHttp (HU-W17, 17.8): `POST /v1/firmas` de adaptador-firma con httpx.MockTransport.

Solo un 200 con el `FirmaDto` del contrato (`contracts/openapi/adaptador-firma.yaml`), el mismo
`documentoId` y la misma huella es una firma. Lo demás es `FalloFirma` con un código propio de la
póliza: `FIRMA_TIEMPO_AGOTADO` (timeout propio o 504), `FIRMA_NO_DISPONIBLE` (sin conexión o 5xx) y
`FIRMA_RESPUESTA_INVALIDA` (502, cualquier otro estado o un 200 fuera de contrato). Los timeouts,
5xx y cuerpos malformados van aquí, no en el pacto.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from polizas.adapters.outbound.firma_http import AdaptadorFirmaHttp
from polizas.application.poliza.puertos import FirmaObtenida, SolicitudFirma
from polizas.domain.poliza.errores import FalloFirma
from solventa_common import correlation

pytestmark = pytest.mark.anyio
FIRMANTE = UUID("00000000-0000-4000-8000-000000000001")
HUELLA = "sha256:" + "ab" * 32
BASE = "http://adaptador-firma:8080"
Responder = Callable[[httpx.Request], httpx.Response]


def solicitud(**cambios: Any) -> SolicitudFirma:
    datos: dict[str, Any] = {
        "documento_id": uuid4(),
        "huella": HUELLA,
        "firmante_ref": FIRMANTE,
        "correlation_id": "corr-w17-firma",
    }
    datos.update(cambios)
    return SolicitudFirma(**datos)


class Registro:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.solicitudes: list[httpx.Request] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.solicitudes.append(request)
        self.timeouts.append(request.extensions.get("timeout"))
        return self.responder(request)


async def firmar(
    responder: Responder | Any, entrada: SolicitudFirma | None = None, timeout_ms: int = 6500
) -> tuple[Any, Registro]:
    """El resultado (una FirmaObtenida o el FalloFirma) y lo que recibió el adaptador."""
    registro = Registro(responder) if callable(responder) else responder
    entrada = entrada or solicitud()
    async with correlation.cliente_async(transport=httpx.MockTransport(registro)) as cliente:
        try:
            resultado: Any = await AdaptadorFirmaHttp(cliente, BASE + "/", timeout_ms).firmar(
                entrada
            )
        except FalloFirma as fallo:
            resultado = fallo
    return resultado, registro


def firma_dto(documento_id: Any, huella: Any = HUELLA, **cambios: Any) -> dict[str, Any]:
    return {
        "documentoId": str(documento_id),
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": "2026-01-01T00:00:00Z",
        "huella": huella,
    } | cambios


def eco(**cambios: Any) -> Responder:
    """El adaptador firma lo que recibió (como el simulador, con plantilla)."""

    def responder(request: httpx.Request) -> httpx.Response:
        cuerpo = json.loads(request.content)
        return httpx.Response(
            200, json=firma_dto(cuerpo["documentoId"], cuerpo["huella"]) | cambios
        )

    return responder


def error(estado: int, codigo: str) -> Responder:
    return lambda request: httpx.Response(
        estado, json={"codigo": codigo, "mensaje": "x", "correlationId": None, "detalles": {}}
    )


async def test_un_200_del_contrato_es_la_firma() -> None:
    entrada = solicitud()
    resultado, registro = await firmar(eco(), entrada)
    assert resultado == FirmaObtenida(
        documento_id=entrada.documento_id,
        huella=HUELLA,
        firma_id="FIR-SINT-C01",
        sello_tiempo=datetime(2026, 1, 1, tzinfo=UTC),
    )
    [pedido] = registro.solicitudes
    assert (pedido.method, str(pedido.url)) == ("POST", f"{BASE}/v1/firmas")
    assert json.loads(pedido.content) == {
        "documentoId": str(entrada.documento_id),
        "huella": HUELLA,
        "firmanteRef": str(FIRMANTE),
    }
    assert pedido.headers["X-Correlation-Id"] == "corr-w17-firma"
    assert pedido.headers["Content-Type"] == "application/json"


async def test_el_sello_conserva_la_zona_del_proveedor() -> None:
    resultado, _ = await firmar(eco(selloTiempo="2026-01-01T07:00:00+07:00"))
    assert resultado.sello_tiempo == datetime(2026, 1, 1, 7, tzinfo=timezone(timedelta(hours=7)))


async def test_el_timeout_es_de_6500_ms_por_fase() -> None:
    _, registro = await firmar(eco())
    [timeout] = registro.timeouts
    assert timeout == {"connect": 6.5, "read": 6.5, "write": 6.5, "pool": 6.5}


async def test_el_timeout_es_configurable() -> None:
    _, registro = await firmar(eco(), timeout_ms=7000)
    assert registro.timeouts[0]["read"] == 7.0


@pytest.mark.parametrize(
    ("responder", "codigo"),
    [
        (error(504, "TIEMPO_AGOTADO"), FalloFirma.TIEMPO_AGOTADO),
        (error(503, "FUENTE_NO_DISPONIBLE"), FalloFirma.NO_DISPONIBLE),
        (error(500, "ERROR_INTERNO"), FalloFirma.NO_DISPONIBLE),
        (error(502, "RESPUESTA_INVALIDA"), FalloFirma.RESPUESTA_INVALIDA),
        (error(422, "SOLICITUD_INVALIDA"), FalloFirma.RESPUESTA_INVALIDA),
        (error(404, "NO_ENCONTRADO"), FalloFirma.RESPUESTA_INVALIDA),
        (
            lambda r: httpx.Response(302, headers={"Location": "/otra"}),
            FalloFirma.RESPUESTA_INVALIDA,
        ),
        (lambda r: httpx.Response(200, content=b'{"estado": '), FalloFirma.RESPUESTA_INVALIDA),
        (lambda r: httpx.Response(200, json=[]), FalloFirma.RESPUESTA_INVALIDA),
        (eco(estado="PENDIENTE"), FalloFirma.RESPUESTA_INVALIDA),
        (eco(firmaId=""), FalloFirma.RESPUESTA_INVALIDA),
        (eco(firmaId="   "), FalloFirma.RESPUESTA_INVALIDA),
        (eco(firmaId=7), FalloFirma.RESPUESTA_INVALIDA),
        (eco(selloTiempo="2026-01-01T00:00:00"), FalloFirma.RESPUESTA_INVALIDA),
        (eco(selloTiempo="ayer"), FalloFirma.RESPUESTA_INVALIDA),
        (eco(huella="sha256:" + "cd" * 32), FalloFirma.RESPUESTA_INVALIDA),
        (eco(documentoId=str(uuid4())), FalloFirma.RESPUESTA_INVALIDA),
        (eco(documentoId="no-uuid"), FalloFirma.RESPUESTA_INVALIDA),
    ],
    ids=[
        "504",
        "503",
        "500",
        "502",
        "422",
        "404",
        "302",
        "json_invalido",
        "no_es_objeto",
        "otro_estado",
        "firma_vacia",
        "firma_en_blanco",
        "firma_no_texto",
        "sello_sin_zona",
        "sello_invalido",
        "otra_huella",
        "otro_documento",
        "documento_no_uuid",
    ],
)
async def test_todo_lo_que_no_es_la_firma_es_un_fallo_con_codigo(
    responder: Responder, codigo: str
) -> None:
    resultado, registro = await firmar(responder)
    assert isinstance(resultado, FalloFirma)
    assert resultado.codigo == codigo
    # Sin reintentos: la emisión sigue EMITIENDO y la reintenta la tarea emisora.
    assert len(registro.solicitudes) == 1


async def test_un_campo_de_mas_en_la_respuesta_se_ignora() -> None:
    resultado, _ = await firmar(eco(proveedor="sintetico"))
    assert isinstance(resultado, FirmaObtenida)


@pytest.mark.parametrize(
    "excepcion",
    [httpx.ReadTimeout("lento"), httpx.ConnectTimeout("lento")],
    ids=["lectura", "conexion"],
)
async def test_un_timeout_de_httpx_es_tiempo_agotado(excepcion: Exception) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise excepcion

    resultado, registro = await firmar(responder)
    assert resultado.codigo == FalloFirma.TIEMPO_AGOTADO
    assert len(registro.solicitudes) == 1


@pytest.mark.parametrize(
    "excepcion",
    [httpx.ConnectError("caído"), httpx.RemoteProtocolError("cortado")],
    ids=["conexion", "protocolo"],
)
async def test_sin_conexion_es_no_disponible(excepcion: Exception) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise excepcion

    resultado, _ = await firmar(responder)
    assert resultado.codigo == FalloFirma.NO_DISPONIBLE


async def test_el_corte_duro_cubre_la_llamada_completa() -> None:
    # Un transporte que no respeta el timeout de HTTPX: el corte total lo detiene.
    class Lento(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200)

    async with correlation.cliente_async(transport=Lento()) as cliente:
        with pytest.raises(FalloFirma) as fallo:
            await asyncio.wait_for(AdaptadorFirmaHttp(cliente, BASE, 50).firmar(solicitud()), 2)
    assert fallo.value.codigo == FalloFirma.TIEMPO_AGOTADO


def test_los_codigos_tienen_forma_de_codigo_y_son_de_la_poliza() -> None:
    import re

    codigos = {FalloFirma.TIEMPO_AGOTADO, FalloFirma.NO_DISPONIBLE, FalloFirma.RESPUESTA_INVALIDA}
    assert codigos == {"FIRMA_TIEMPO_AGOTADO", "FIRMA_NO_DISPONIBLE", "FIRMA_RESPUESTA_INVALIDA"}
    # No son los del catálogo de adaptador-firma (TIEMPO_AGOTADO, FUENTE_NO_DISPONIBLE...).
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", c) for c in codigos)
    assert str(FalloFirma(FalloFirma.NO_DISPONIBLE)) == "FIRMA_NO_DISPONIBLE"


async def test_no_registra_el_firmante_ni_la_huella(
    capturar_logs: Callable[..., Any],
) -> None:
    salida = capturar_logs()
    await firmar(eco())

    def lento(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento")

    await firmar(lento)
    texto = salida.getvalue()
    assert str(FIRMANTE) not in texto and HUELLA.removeprefix("sha256:") not in texto
