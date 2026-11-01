"""Cliente del proveedor de firma (FirmaHttp) contra respx: encabezados, cuerpo y cada respuesta."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from adaptador_firma.adapters.outbound.firma_http import FirmaHttp
from adaptador_firma.domain.firmas.modelos import (
    EstadoFirma,
    Firma,
    RespuestaInvalida,
    SolicitudFirma,
)
from pydantic import SecretStr

pytestmark = pytest.mark.anyio

BASE = "http://sim/firma"
DOCUMENTO = UUID("5b1c9e2a-7f3d-4a60-8c11-000000000d01")
FIRMANTE = UUID("00000000-0000-4000-8000-000000000001")
HUELLA = "sha256:" + "0123456789abcdef" * 4
API_KEY = "clave-sintetica-firma"
SOLICITUD = SolicitudFirma(documento_id=DOCUMENTO, huella=HUELLA, firmante_ref=FIRMANTE)
SELLO = "2026-01-01T00:00:00Z"


def _respuesta(**cambios: Any) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "documentoId": str(DOCUMENTO),
        "estado": "FIRMADO",
        "firmaId": "FIR-SINT-C01",
        "selloTiempo": SELLO,
        "huella": HUELLA,
    }
    cuerpo.update(cambios)
    return cuerpo


def _sin(clave: str) -> dict[str, Any]:
    return {k: v for k, v in _respuesta().items() if k != clave}


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _proveedor() -> FirmaHttp:
    return FirmaHttp(httpx.AsyncClient(), BASE, SecretStr(API_KEY))


async def _firmar() -> Firma:
    return await _proveedor().firmar(SOLICITUD, 5.0)


async def test_envia_encabezados_y_cuerpo_exactos(mock: respx.MockRouter) -> None:
    ruta = mock.post("/v1/firmas").respond(200, json=_respuesta())
    firma = await _firmar()
    assert firma == Firma(
        documento_id=DOCUMENTO,
        estado=EstadoFirma.FIRMADO,
        firma_id="FIR-SINT-C01",
        sello_tiempo=datetime(2026, 1, 1, tzinfo=UTC),
        huella=HUELLA,
    )
    request = ruta.calls.last.request
    assert request.url == f"{BASE}/v1/firmas"
    # La clave idempotente es el documento: reintentar la misma firma no firma otra vez.
    assert request.headers["Idempotency-Key"] == str(DOCUMENTO)
    assert request.headers["X-Api-Key"] == API_KEY
    assert json.loads(request.content) == {
        "documentoId": str(DOCUMENTO),
        "huella": HUELLA,
        "firmante": str(FIRMANTE),
    }
    assert request.extensions["timeout"]["read"] == 5.0


async def test_el_documento_se_compara_como_uuid(mock: respx.MockRouter) -> None:
    mock.post("/v1/firmas").respond(200, json=_respuesta(documentoId=str(DOCUMENTO).upper()))
    assert (await _firmar()).documento_id == DOCUMENTO


async def test_el_sello_conserva_la_zona_del_proveedor(mock: respx.MockRouter) -> None:
    """Es la hora del proveedor: no se compara con el reloj local ni se convierte."""
    mock.post("/v1/firmas").respond(200, json=_respuesta(selloTiempo="2030-06-15T08:30:00-05:00"))
    sello = (await _firmar()).sello_tiempo
    assert sello == datetime(2030, 6, 15, 8, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert sello.utcoffset() == timedelta(hours=-5)


# --- Fallos: el cliente deja los de transporte a la protección y traduce los de forma ---------
@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("lento"), httpx.ConnectTimeout("lento"), httpx.ConnectError("caído")],
)
async def test_timeout_y_conexion_se_propagan(mock: respx.MockRouter, error: Exception) -> None:
    mock.post("/v1/firmas").mock(side_effect=error)
    with pytest.raises(type(error)):
        await _firmar()


@pytest.mark.parametrize("estado", [500, 502, 503, 504])
async def test_5xx_lanza_http_status_error(mock: respx.MockRouter, estado: int) -> None:
    mock.post("/v1/firmas").respond(estado, json={"error": "unavailable"})
    with pytest.raises(httpx.HTTPStatusError):
        await _firmar()


@pytest.mark.parametrize("texto", ['{"estado": ', "<html>", ""])
async def test_cuerpo_malformado(mock: respx.MockRouter, texto: str) -> None:
    mock.post("/v1/firmas").respond(200, text=texto)
    with pytest.raises(RespuestaInvalida) as error:
        await _firmar()
    assert error.value.motivo == "JSON"


FUERA_DE_FORMA = [
    pytest.param([], id="lista"),
    pytest.param("FIRMADO", id="texto"),
    # Estado
    pytest.param(_respuesta(estado="RECHAZADO"), id="estado-rechazado"),
    pytest.param(_respuesta(estado="PENDIENTE"), id="estado-pendiente"),
    pytest.param(_respuesta(estado="firmado"), id="estado-minusculas"),
    pytest.param(_respuesta(estado=None), id="estado-nulo"),
    pytest.param(_sin("estado"), id="sin-estado"),
    # Documento: el mismo que se envió
    pytest.param(
        _respuesta(documentoId="00000000-0000-4000-8000-0000000000ff"), id="otro-documento"
    ),
    pytest.param(_respuesta(documentoId="doc-sintetico-firmado"), id="documento-no-uuid"),
    pytest.param(_respuesta(documentoId=1), id="documento-numero"),
    pytest.param(_sin("documentoId"), id="sin-documento"),
    # Huella: la misma que se envió, texto exacto
    pytest.param(_respuesta(huella="sha256:" + "f" * 64), id="otra-huella"),
    pytest.param(_respuesta(huella=HUELLA.upper()), id="huella-mayusculas"),
    pytest.param(_respuesta(huella=HUELLA.removeprefix("sha256:")), id="huella-sin-prefijo"),
    pytest.param(_respuesta(huella=None), id="huella-nula"),
    pytest.param(_sin("huella"), id="sin-huella"),
    # firmaId no vacío
    pytest.param(_respuesta(firmaId=""), id="firma-vacia"),
    pytest.param(_respuesta(firmaId="   "), id="firma-en-blanco"),
    pytest.param(_respuesta(firmaId=None), id="firma-nula"),
    pytest.param(_respuesta(firmaId=123), id="firma-numero"),
    pytest.param(_sin("firmaId"), id="sin-firma"),
    # selloTiempo con zona
    pytest.param(_respuesta(selloTiempo="2026-01-01T00:00:00"), id="sello-sin-zona"),
    pytest.param(_respuesta(selloTiempo="2026-01-01"), id="sello-solo-fecha"),
    pytest.param(_respuesta(selloTiempo="ayer"), id="sello-texto"),
    pytest.param(_respuesta(selloTiempo=""), id="sello-vacio"),
    pytest.param(_respuesta(selloTiempo=1767225600), id="sello-epoch"),
    pytest.param(_respuesta(selloTiempo=None), id="sello-nulo"),
    pytest.param(_sin("selloTiempo"), id="sin-sello"),
]


@pytest.mark.parametrize("cuerpo", FUERA_DE_FORMA)
async def test_cuerpo_fuera_de_forma(mock: respx.MockRouter, cuerpo: Any) -> None:
    mock.post("/v1/firmas").respond(200, json=cuerpo)
    with pytest.raises(RespuestaInvalida) as error:
        await _firmar()
    assert error.value.motivo == "JSON"


async def test_404_documento_no_reconocido_es_respuesta_invalida(mock: respx.MockRouter) -> None:
    mock.post("/v1/firmas").respond(404, json={"error": "documento_no_reconocido"})
    with pytest.raises(RespuestaInvalida) as error:
        await _firmar()
    assert error.value.motivo == "HTTP_404"


@pytest.mark.parametrize("estado", [201, 204, 302, 400, 401, 409, 422])
async def test_otro_estado_es_respuesta_invalida(mock: respx.MockRouter, estado: int) -> None:
    mock.post("/v1/firmas").respond(estado, json=_respuesta())
    with pytest.raises(RespuestaInvalida) as error:
        await _firmar()
    assert error.value.motivo == f"HTTP_{estado}"
