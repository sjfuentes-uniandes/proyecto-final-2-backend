"""HU-W04 (N1): la oferta en el contrato público del socio.

- POST /cotizaciones → 201 con la oferta v1 (lista blanca de campos), 200 al repetir la clave.
- GET /cotizaciones/{id} → la oferta del socio; de otro socio, 404 OFERTA_NO_ENCONTRADA.
- Una oferta vencida nunca se entrega: 410 OFERTA_VENCIDA con detalles.accion = NUEVA_COTIZACION.
- Los rechazos de cotizacion (HU-W03) pasan con el mismo código; lo demás es 503 sin detalles.
- Se mide `socio.cotizar` y `socio.consultar_oferta`, y los logs no llevan datos del asegurado.
"""

import copy
import json
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from api_socios.adapters.inbound.dependencias import consultar_oferta, cotizar_oferta
from api_socios.adapters.inbound.socio import OfertaV1
from api_socios.adapters.outbound.cotizacion_http import CotizacionHttp
from api_socios.application.ofertas import ConsultarOferta, CotizarOferta
from api_socios.main import app
from solventa_common import correlation

COTIZACION_ID = "00000000-0000-4000-d000-000000000001"
OFERTA_ID = "00000000-0000-4000-e000-000000000001"
CLAVE = "00000000-0000-4000-c000-000000000001"
SOLICITUD = {
    "clienteId": "00000000-0000-4000-8000-000000000001",
    "productoId": "vida-hipotecario",
    "datosAsegurado": {
        "fechaNacimiento": "1990-04-18",
        "plazoCreditoMeses": 180,
        "sumaAsegurada": 180000000,
    },
    "coberturasOpcionales": ["ENFERMEDADES_GRAVES"],
}
OFERTA = {
    "id": OFERTA_ID,
    "codigo": "COT-2026-00001",
    "cotizacionId": COTIZACION_ID,
    "precio": "97460",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "coberturas": [
        {"codigo": "MUERTE", "nombre": "Muerte por cualquier causa", "sumaAsegurada": "180000000"},
        {
            "codigo": "ENFERMEDADES_GRAVES",
            "nombre": "Enfermedades graves",
            "sumaAsegurada": "54000000",
        },
    ],
    "exclusionesResumidas": [
        {"codigo": "SUICIDIO_PRIMER_ANIO", "descripcion": "Suicidio durante el primer año"}
    ],
    "version": 1,
    "versionReglas": "2026.10.1",
    "creadaEn": "2026-10-22T15:00:00Z",
    "vigenteHasta": "2026-10-29T15:00:00Z",
    "estado": "VIGENTE",
}
# La respuesta completa de cotizacion: con todo lo interno que el socio no debe recibir.
COTIZACION = {
    "id": COTIZACION_ID,
    "clienteId": SOLICITUD["clienteId"],
    "socioId": "SOC-000001",
    "productoId": "vida-hipotecario",
    "versionProducto": "2026.09.1",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "versionReglas": "2026.10.1",
    "perfilId": None,
    "estado": "CALCULADA",
    "coberturas": [],
    "entradas": {"sumaAsegurada": "180000000", "edad": 36, "nivelRiesgo": None},
    "desglose": {"primaBase": "75600", "factores": [], "primaTotal": "97460"},
    "primaTotal": "97460",
    "huellaCalculo": "sha256:abc",
    "calculadoEn": "2026-10-22T15:00:00Z",
    "duracionMs": 40,
    "presupuestoMs": 250,
    "pasos": [],
    "vigenteHasta": OFERTA["vigenteHasta"],
    "oferta": OFERTA,
}
CAMPOS_V1 = {
    "id",
    "codigo",
    "cotizacionId",
    "productoId",
    "precio",
    "moneda",
    "periodicidad",
    "coberturas",
    "exclusionesResumidas",
    "vigenteHasta",
    "estado",
    "version",
    "versionReglas",
    "creadaEn",
}
INTERNOS = {
    "huellaCalculo",
    "hashReglas",
    "entradas",
    "desglose",
    "perfilId",
    "socioId",
    "clienteId",
    "pasos",
    "duracionMs",
    "presupuestoMs",
    "edad",
    "nivelRiesgo",
}


def _claves(valor: Any) -> set[str]:
    """Todas las claves del JSON, también las anidadas."""
    if isinstance(valor, dict):
        return set(valor) | {c for v in valor.values() for c in _claves(v)}
    if isinstance(valor, list):
        return {c for v in valor for c in _claves(v)}
    return set()


def _respuesta(cotizacion: dict[str, Any], estado: int = 201) -> httpx.Response:
    return httpx.Response(estado, json=cotizacion)


def _vencida() -> dict[str, Any]:
    vencida = copy.deepcopy(COTIZACION)
    vencida["oferta"]["estado"] = "VENCIDA"
    return vencida


class CotizacionFalsa:
    """cotizacion simulada: responde según el método; `respuesta` puede ser una excepción."""

    def __init__(self) -> None:
        self.solicitudes: list[httpx.Request] = []
        self.post: httpx.Response | Exception = _respuesta(COTIZACION)
        self.get: httpx.Response | Exception = _respuesta(COTIZACION, 200)

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        respuesta = self.post if solicitud.method == "POST" else self.get
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


@pytest.fixture
def cotizacion(entorno: Any) -> Iterator[CotizacionFalsa]:
    falsa = CotizacionFalsa()
    adaptador = CotizacionHttp("http://cotizacion:8080", 1500, httpx.MockTransport(falsa))
    app.dependency_overrides[cotizar_oferta] = lambda: CotizarOferta(adaptador)
    app.dependency_overrides[consultar_oferta] = lambda: ConsultarOferta(adaptador)
    yield falsa


@pytest.fixture
def socio(entorno: Any, encabezados: Callable[..., dict[str, str]]) -> dict[str, str]:
    alta = entorno.cliente.post(
        "/admin/socios",
        json={
            "nombre": "Fintech Caribe S.A.S.",
            "canal": "plataforma_digital",
            "plan": "basico",
            "permisos": ["cotizar"],
        },
    )
    assert alta.status_code == 201, alta.text
    return encabezados(entorno.repositorio.obtener(alta.json()["id"]), "cotizar")


def _cotizar(entorno: Any, socio: dict[str, str], **cambios: Any) -> httpx.Response:
    return entorno.cliente.post(
        "/cotizaciones",
        json=cambios.pop("json", SOLICITUD),
        headers={**socio, "Idempotency-Key": CLAVE, **cambios},
    )


# --- POST /cotizaciones ---------------------------------------------------------------------


def test_ac1_cotizar_devuelve_la_oferta_publica(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    respuesta = _cotizar(entorno, socio)
    assert respuesta.status_code == 201
    oferta = respuesta.json()
    # AC1 y la regla "solo los datos necesarios": exactamente la lista blanca.
    assert set(oferta) == CAMPOS_V1
    assert not INTERNOS & _claves(oferta)
    assert {k: v for k, v in oferta.items() if k != "productoId"} == OFERTA
    assert oferta["productoId"] == "vida-hipotecario"


def test_reenvia_como_socio_con_la_clave_y_sin_campos_del_back_office(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    _cotizar(entorno, socio, **{"X-Correlation-Id": "corr-socio-w04"})
    (enviada,) = cotizacion.solicitudes
    assert (enviada.method, enviada.url.path) == ("POST", "/v1/cotizaciones")
    assert enviada.headers["X-Actor-Id"] == "SOC-000001"
    assert enviada.headers["X-Actor-Rol"] == "SOCIO"
    assert enviada.headers["Idempotency-Key"] == CLAVE
    assert enviada.headers["X-Correlation-Id"] == "corr-socio-w04"
    assert json.loads(enviada.read()) == SOLICITUD


def test_misma_clave_devuelve_200(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    cotizacion.post = _respuesta(COTIZACION, 200)
    assert _cotizar(entorno, socio).status_code == 200


@pytest.mark.parametrize(
    "cuerpo",
    [
        SOLICITUD | {"perfilId": "00000000-0000-4000-a000-000000000001"},
        SOLICITUD | {"socioId": "SOC-000002"},
        {k: v for k, v in SOLICITUD.items() if k != "clienteId"},
        SOLICITUD | {"datosAsegurado": SOLICITUD["datosAsegurado"] | {"edad": 36}},
    ],
)
def test_solicitud_invalida_sin_llamar_a_cotizacion(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa, cuerpo: dict[str, Any]
) -> None:
    respuesta = _cotizar(entorno, socio, json=cuerpo)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert cotizacion.solicitudes == []


def test_la_clave_de_idempotencia_es_obligatoria(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    respuesta = entorno.cliente.post("/cotizaciones", json=SOLICITUD, headers=socio)
    assert respuesta.status_code == 422
    assert "Idempotency-Key" in str(respuesta.json()["detalles"]["campos"])


@pytest.mark.parametrize(
    ("estado", "codigo"),
    [
        (422, "PRODUCTO_NO_ENCONTRADO"),
        (422, "PRODUCTO_INACTIVO"),
        (422, "PRODUCTO_FUERA_DE_VIGENCIA"),
        (422, "REGLAS_NO_VIGENTES"),
        (422, "DATOS_FUERA_DE_RANGO"),
        (422, "COBERTURA_NO_DISPONIBLE"),
        (422, "IDEMPOTENCIA_CONFLICTO"),
        (404, "CLIENTE_NO_ENCONTRADO"),
    ],
)
def test_los_rechazos_de_w03_pasan_con_el_mismo_codigo(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa, estado: int, codigo: str
) -> None:
    cotizacion.post = httpx.Response(
        estado,
        json={
            "codigo": codigo,
            "mensaje": "m",
            "detalles": {"campos": ["datosAsegurado.sumaAsegurada"], "interno": "x"},
        },
    )
    respuesta = _cotizar(entorno, socio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (estado, codigo)
    # Solo las rutas de los campos; nada más de cotizacion.
    assert respuesta.json()["detalles"] == {"campos": ["datosAsegurado.sumaAsegurada"]}


@pytest.mark.parametrize(
    "falla",
    [
        httpx.Response(
            503,
            json={
                "codigo": "DEPENDENCIA_NO_DISPONIBLE",
                "detalles": {"dependencia": "catalogo.producto"},
            },
        ),
        httpx.Response(403, json={"codigo": "ROL_NO_AUTORIZADO"}),
        httpx.Response(422, json={"codigo": "PERFIL_NO_CORRESPONDE"}),
        httpx.Response(500, text="error"),
        httpx.Response(201, json={"productoId": "vida-hipotecario", "oferta": None}),
        httpx.Response(201, json={"productoId": "vida-hipotecario", "oferta": {"id": "x"}}),
        httpx.ReadTimeout("lenta"),
        httpx.ConnectError("caida"),
    ],
)
def test_lo_demas_es_503_sin_revelar_la_topologia(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa, falla: Any
) -> None:
    cotizacion.post = falla
    respuesta = _cotizar(entorno, socio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")
    assert respuesta.json()["detalles"] == {}
    assert "catalogo" not in respuesta.text


def test_ac3_repetir_la_clave_de_una_oferta_vencida_es_410(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    cotizacion.post = _respuesta(_vencida(), 200)
    respuesta = _cotizar(entorno, socio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (410, "OFERTA_VENCIDA")


# --- GET /cotizaciones/{id} -----------------------------------------------------------------


def test_consultar_la_oferta_vigente(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    respuesta = entorno.cliente.get(f"/cotizaciones/{COTIZACION_ID}", headers=socio)
    assert respuesta.status_code == 200
    assert set(respuesta.json()) == CAMPOS_V1
    (enviada,) = cotizacion.solicitudes
    assert enviada.url.path == f"/v1/cotizaciones/{COTIZACION_ID}"
    assert (enviada.headers["X-Actor-Id"], enviada.headers["X-Actor-Rol"]) == (
        "SOC-000001",
        "SOCIO",
    )


def test_ac3_oferta_vencida_es_410_con_la_accion(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    cotizacion.get = _respuesta(_vencida(), 200)
    respuesta = entorno.cliente.get(f"/cotizaciones/{COTIZACION_ID}", headers=socio)
    assert respuesta.status_code == 410
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == "OFERTA_VENCIDA"
    assert cuerpo["detalles"] == {
        "accion": "NUEVA_COTIZACION",
        "ofertaId": OFERTA_ID,
        "cotizacionId": COTIZACION_ID,
        "vigenteHasta": "2026-10-29T15:00:00+00:00",
    }
    # Sin el precio vencido.
    assert "97460" not in respuesta.text


def test_seguridad_la_oferta_de_otro_socio_es_404(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    """cotizacion responde COTIZACION_NO_ENCONTRADA para la de otro socio (no revela que existe)."""
    cotizacion.get = httpx.Response(
        404, json={"codigo": "COTIZACION_NO_ENCONTRADA", "mensaje": "m"}
    )
    respuesta = entorno.cliente.get(f"/cotizaciones/{COTIZACION_ID}", headers=socio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "OFERTA_NO_ENCONTRADA")


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(404, json={"codigo": "OTRO"}),
        httpx.Response(404, text="no json"),
        httpx.Response(500, json={}),
    ],
)
def test_otro_fallo_al_consultar_es_503(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa, respuesta: httpx.Response
) -> None:
    cotizacion.get = respuesta
    leida = entorno.cliente.get(f"/cotizaciones/{COTIZACION_ID}", headers=socio)
    assert (leida.status_code, leida.json()["codigo"]) == (503, "SERVICIO_NO_DISPONIBLE")


def test_una_cotizacion_sin_oferta_es_404(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    cotizacion.get = _respuesta(COTIZACION | {"oferta": None}, 200)
    respuesta = entorno.cliente.get(f"/cotizaciones/{COTIZACION_ID}", headers=socio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (404, "OFERTA_NO_ENCONTRADA")


def test_un_id_que_no_es_uuid_es_422(
    entorno: Any, socio: dict[str, str], cotizacion: CotizacionFalsa
) -> None:
    respuesta = entorno.cliente.get("/cotizaciones/COT-2026-00001", headers=socio)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert cotizacion.solicitudes == []


# --- Observabilidad y PII -------------------------------------------------------------------


def test_mide_las_operaciones_y_no_registra_pii(
    entorno: Any,
    socio: dict[str, str],
    cotizacion: CotizacionFalsa,
    capturar_logs: Callable[[], Any],
) -> None:
    salida = capturar_logs()
    _cotizar(entorno, socio)
    cotizacion.get = _respuesta(_vencida(), 200)
    entorno.cliente.get(f"/cotizaciones/{COTIZACION_ID}", headers=socio)
    lineas = [json.loads(linea) for linea in salida.getvalue().splitlines()]
    operaciones = {(r.get("operation"), r.get("result")) for r in lineas}
    assert ("socio.cotizar", "ok") in operaciones
    assert ("socio.consultar_oferta", "rechazado") in operaciones
    texto = salida.getvalue()
    for centinela in ("1990-04-18", "180000000", "fechaNacimiento", "sumaAsegurada"):
        assert centinela not in texto, centinela


# --- DTO público ------------------------------------------------------------------------------


def test_el_dto_publico_es_la_lista_blanca() -> None:
    assert set(OfertaV1.model_json_schema(by_alias=True)["properties"]) == CAMPOS_V1
    assert UUID(OFERTA_ID)  # el id público es el de la oferta, no el de un perfil o actor


def test_el_cliente_http_cierra_ante_un_cuerpo_que_no_es_json(entorno: Any) -> None:
    del entorno
    import asyncio

    from api_socios.domain.ofertas import CotizacionNoDisponible

    adaptador = CotizacionHttp(
        "http://cotizacion:8080/", 100, httpx.MockTransport(lambda _: httpx.Response(200, text="x"))
    )
    token = correlation.establecer("corr-x")
    try:
        with pytest.raises(CotizacionNoDisponible):
            asyncio.run(adaptador.oferta(UUID(COTIZACION_ID), "SOC-000001"))
    finally:
        correlation.restablecer(token)
