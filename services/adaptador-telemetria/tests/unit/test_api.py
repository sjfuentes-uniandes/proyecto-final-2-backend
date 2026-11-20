"""`POST /v1/eventos` (HU-W21, DS3-16) con el registro y el archivo en memoria: códigos, formato de
los errores y lectura del cuerpo crudo."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from adaptador_telemetria.application.telemetria.puertos import DependenciaNoDisponible
from adaptador_telemetria.config import Settings
from adaptador_telemetria.domain.telemetria.firma import calcular_firma
from adaptador_telemetria.main import crear_app
from fastapi.testclient import TestClient

SECRETO = "secreto-de-prueba"
AHORA = datetime(2026, 11, 10, 14, 0, 5, tzinfo=UTC)

RUTA = "/v1/eventos"


@pytest.fixture
def api(archivo: Any, registro: Any, reloj_fijo: Any) -> Iterator[TestClient]:
    settings = Settings(ALLY_TELEMETRIA_CREDENTIALS=json.dumps({"hmac_secret": SECRETO}))
    app = crear_app(settings, reloj=reloj_fijo, archivo=archivo, registro=registro)
    with TestClient(app) as cliente:
        yield cliente


def _enviar(api: TestClient, envio: Any, **encabezados: str) -> Any:
    return api.post(RUTA, content=envio.cuerpo, headers={**envio.encabezados(), **encabezados})


def test_un_evento_valido_responde_202_con_su_id_y_el_instante(
    api: TestClient,
    archivo: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    respuesta = _enviar(api, firmar_caso(casos["valido"], AHORA))
    assert respuesta.status_code == 202
    assert respuesta.json() == {
        "eventoId": casos["valido"]["esperado"]["eventoId"],
        "recibidoEn": "2026-11-10T14:00:05Z",
    }
    assert len(archivo.guardados) == 1


def test_el_reenvio_responde_200_con_el_mismo_evento(
    api: TestClient,
    registro: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    primera = _enviar(api, firmar_caso(casos["valido"], AHORA))
    segunda = _enviar(api, firmar_caso(casos["duplicado"], AHORA))
    assert (primera.status_code, segunda.status_code) == (202, 200)
    assert segunda.json() == primera.json()
    assert len(registro.outbox) == 1


def test_cada_rechazo_responde_su_codigo_estable(
    api: TestClient,
    archivo: Any,
    registro: Any,
    datos: dict[str, Any],
    firmar_caso: Callable[..., Any],
) -> None:
    rechazados = [c for c in datos["eventos"] if c["esperado"]["http"]["codigo"]]
    for entrada in rechazados:
        esperado = entrada["esperado"]["http"]
        respuesta = _enviar(api, firmar_caso(entrada, AHORA), **{"X-Correlation-Id": "corr-prueba"})
        cuerpo = respuesta.json()
        assert (respuesta.status_code, cuerpo["codigo"]) == (
            esperado["estado"],
            esperado["codigo"],
        ), entrada["clave"]
        assert set(cuerpo) == {"codigo", "mensaje", "correlationId", "detalles"}
        assert cuerpo["correlationId"] == "corr-prueba"
        if esperado["codigo"] == "CONTRATO_INVALIDO":
            # Siempre `campos`; vacío si el cuerpo ni siquiera es un objeto JSON.
            assert cuerpo["detalles"] == {"campos": esperado.get("campos", [])}, entrada["clave"]
        else:
            assert cuerpo["detalles"] == {}, entrada["clave"]
    assert archivo.guardados == [] and registro.llamadas == []


def test_un_cuerpo_malformado_con_firma_invalida_es_401_y_no_422(
    api: TestClient, casos: dict[str, dict[str, Any]], firmar_caso: Callable[..., Any]
) -> None:
    """Por eso la ruta lee el cuerpo crudo y no lo declara como parámetro."""
    envio = firmar_caso(casos["malformadoNoJson"], AHORA)
    respuesta = api.post(
        RUTA,
        content=envio.cuerpo,
        headers={**envio.encabezados(), "X-Telemetria-Firma": "sha256=00"},
    )
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (401, "FIRMA_INVALIDA")


def test_la_firma_es_sobre_los_bytes_recibidos(api: TestClient) -> None:
    """Un cuerpo reformateado (espacios, saltos de línea) con la firma del original se rechaza; el
    mismo contenido firmado tal como se envía se acepta."""
    cuerpo = {
        "versionContrato": "1",
        "idExterno": "IDEAM-2026-1110-0500",
        "eventoOrigen": "EVT-2026-0913",
        "tipoEvento": "LLUVIA_ACUMULADA",
        "valorObservado": "135",
        "unidad": "mm",
        "zona": "11001",
        "observadoEn": "2026-11-10T14:00:00Z",
    }
    compacto = json.dumps(cuerpo, separators=(",", ":")).encode()
    con_formato = json.dumps(cuerpo, indent=2).encode()
    marca = "2026-11-10T14:00:05Z"
    encabezados = {
        "X-Telemetria-Fuente": "IDEAM-SIM",
        "X-Telemetria-Marca-Tiempo": marca,
        "X-Telemetria-Firma": calcular_firma(SECRETO, marca, compacto),
    }
    assert api.post(RUTA, content=con_formato, headers=encabezados).status_code == 401
    assert api.post(RUTA, content=compacto, headers=encabezados).status_code == 202
    firmado = {**encabezados, "X-Telemetria-Firma": calcular_firma(SECRETO, marca, con_formato)}
    assert api.post(RUTA, content=con_formato, headers=firmado).status_code == 200


def test_la_respuesta_devuelve_el_correlation_id_y_genera_uno_si_falta(
    api: TestClient, casos: dict[str, dict[str, Any]], firmar_caso: Callable[..., Any]
) -> None:
    envio = firmar_caso(casos["firmaInvalida"], AHORA)
    con = _enviar(api, envio, **{"X-Correlation-Id": "corr-x"})
    sin = _enviar(api, envio)
    assert con.headers["X-Correlation-Id"] == "corr-x"
    assert sin.headers["X-Correlation-Id"]


def test_el_correlation_id_llega_al_registro(
    api: TestClient,
    registro: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    llamadas: list[str] = []
    original = registro.registrar

    async def registrar(evento: Any, recibido_en: datetime, ref: str, correlacion: str) -> Any:
        llamadas.append(correlacion)
        return await original(evento, recibido_en, ref, correlacion)

    registro.registrar = registrar  # type: ignore[method-assign]
    _enviar(api, firmar_caso(casos["valido"], AHORA), **{"X-Correlation-Id": "corr-arriba"})
    assert llamadas == ["corr-arriba"]


@pytest.mark.parametrize("falla", ["archivo", "base"])
def test_si_el_archivo_o_la_base_fallan_es_503_y_la_fuente_puede_reenviar(
    api: TestClient,
    archivo: Any,
    registro: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
    falla: str,
) -> None:
    if falla == "archivo":
        archivo.falla = True
    else:
        registro.falla_registro = True
    envio = firmar_caso(casos["valido"], AHORA)
    respuesta = _enviar(api, envio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")
    assert registro.outbox == []
    # Al volver la dependencia, el mismo envío se acepta.
    archivo.falla = registro.falla_registro = False
    assert _enviar(api, envio).status_code == 202


def test_sin_credenciales_toda_peticion_es_401(
    archivo: Any,
    registro: Any,
    reloj_fijo: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    app = crear_app(Settings(), reloj=reloj_fijo, archivo=archivo, registro=registro)
    with TestClient(app) as api:
        respuesta = _enviar(api, firmar_caso(casos["valido"], AHORA))
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (401, "FIRMA_INVALIDA")
    assert archivo.guardados == []


def test_sin_base_ni_archivo_configurados_no_se_acepta_nada(
    reloj_fijo: Any, casos: dict[str, dict[str, Any]], firmar_caso: Callable[..., Any]
) -> None:
    settings = Settings(ALLY_TELEMETRIA_CREDENTIALS=json.dumps({"hmac_secret": SECRETO}))
    with TestClient(crear_app(settings, reloj=reloj_fijo)) as api:
        respuesta = _enviar(api, firmar_caso(casos["valido"], AHORA))
        salud = api.get("/health")
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "DEPENDENCIA_NO_DISPONIBLE")
    assert salud.json() == {"status": "ok"}


def test_el_metodo_y_la_ruta_responden_con_el_formato_estable(api: TestClient) -> None:
    assert api.get(RUTA).status_code == 405
    assert api.get(RUTA).json()["codigo"] == "METODO_NO_PERMITIDO"
    assert api.post("/v1/otro").json()["codigo"] == "NO_ENCONTRADO"


def test_dependencia_no_disponible_no_filtra_la_causa(
    api: TestClient,
    archivo: Any,
    casos: dict[str, dict[str, Any]],
    firmar_caso: Callable[..., Any],
) -> None:
    async def guardar(clave: str, cuerpo: bytes, metadatos: Any) -> None:
        raise DependenciaNoDisponible("archivo")

    archivo.guardar = guardar  # type: ignore[method-assign]
    cuerpo = _enviar(api, firmar_caso(casos["valido"], AHORA)).json()
    assert cuerpo["detalles"] == {}
    assert "archivo" not in cuerpo["mensaje"].lower()
