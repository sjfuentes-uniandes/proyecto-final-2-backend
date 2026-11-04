"""UT-W18 (bff-web): estado y listado de pólizas en el portal (HU-W18) con consulta simulada.

Solo `asesores` consulta (los demás grupos, 403 sin llamar a consulta); actor y rol `ASESOR` hacia
consulta; el 404 y el 422 de consulta pasan y todo lo demás (401, 403, 5xx, timeout, caída, cuerpo
fuera de contrato) es 503 SERVICIO_NO_DISPONIBLE sin detalles. Nada del navegador altera la consulta.
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import estado_polizas as ruta
from bff_web.adapters.outbound.clientes import ClientesCliente
from bff_web.adapters.outbound.consulta import ConsultaCliente
from bff_web.application.estado_poliza import EstadoPolizas
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
POLIZA_ID = "00000000-0000-4000-9000-000000000001"
RUTA_ESTADO = f"/v1/polizas/{POLIZA_ID}/estado"
RUTA_LISTADO = "/v1/polizas"
CLIENTE_ID = "00000000-0000-4000-9000-0000000000c1"
ESTADOS = ["NO_INICIADA", "VIGENTE", "POR_VENCER", "VENCIDA"]
DETALLE: dict[str, Any] = {
    "polizaId": POLIZA_ID,
    "numero": "100-2026-000001",
    "estado": "VIGENTE",
    "fechaActualizacion": "2026-10-24T15:00:05.250000Z",
    "producto": "Vida hipotecario",
    "ramo": "Vida",
    "vigencia": {"desde": "2026-10-25", "hasta": "2027-10-24"},
    "prima": "64638.10",
    "sumaAsegurada": "180000000",
    "moneda": "COP",
    "periodicidad": "MENSUAL",
    "emitidaEn": "2026-10-24T15:00:03.500000Z",
    "coberturas": [{"codigo": "MUERTE", "nombre": "Muerte", "obligatoria": True}],
    "clienteId": CLIENTE_ID,
}
TITULAR: dict[str, Any] = {
    "titularNombre": "Natalia González Ruiz",
    "tipoDocumento": "CC",
    "documentoEnmascarado": "1.020.***.456",
}
SIN_TITULAR: dict[str, Any] = dict.fromkeys(TITULAR)
RESUMEN: dict[str, Any] = {
    "polizaId": POLIZA_ID,
    "numero": "100-2026-000001",
    "ramo": "Vida",
    "producto": "Vida hipotecario",
    "prima": "64638.10",
    "moneda": "COP",
    "vigenciaHasta": "2027-10-24",
    "estado": "VIGENTE",
    "fechaActualizacion": "2026-10-24T15:00:05.250000Z",
}
PAGINA: dict[str, Any] = {
    "items": [RESUMEN],
    "total": 1,
    "pagina": 1,
    "tamano": 20,
    "actualizadoEn": "2026-10-24T15:00:05.250000Z",
}
NO_ENCONTRADA = {
    "codigo": "POLIZA_NO_ENCONTRADA",
    "mensaje": "La póliza no existe",
    "correlationId": "corr-consulta",
    "detalles": {},
}


def _usuario(grupos: str, sub: str = "sub-asesor") -> dict[str, str]:
    return {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Authenticated-Sub": sub,
        "X-Correlation-Id": "corr-bff-w18",
    }


ASESOR = _usuario("[asesores]")
OTROS = {
    "suscriptores": _usuario("[suscriptores]"),
    "operacion": _usuario("[operacion]"),
    "administradores": _usuario("[administradores]"),
}


class Simulado:
    def __init__(self) -> None:
        self.pedidos: list[httpx.Request] = []
        self.responder: Callable[[httpx.Request], httpx.Response] = self._por_ruta
        self.pedidos_clientes: list[httpx.Request] = []
        self.responder_clientes: Callable[[httpx.Request], httpx.Response] = lambda _: (
            httpx.Response(
                200,
                json={
                    "nombreCompleto": TITULAR["titularNombre"],
                    "tipoDocumento": TITULAR["tipoDocumento"],
                    "documentoEnmascarado": TITULAR["documentoEnmascarado"],
                },
            )
        )

    def clientes(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos_clientes.append(pedido)
        return self.responder_clientes(pedido)

    @staticmethod
    def _por_ruta(pedido: httpx.Request) -> httpx.Response:
        if pedido.url.path.endswith("/estado"):
            return httpx.Response(200, json=DETALLE)
        return httpx.Response(200, json=PAGINA)

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)


@pytest.fixture
def consulta(monkeypatch: pytest.MonkeyPatch) -> Iterator[Simulado]:
    simulado = Simulado()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    cliente = ConsultaCliente("http://consulta:8080", 700, httpx.MockTransport(simulado))
    clientes = ClientesCliente("http://clientes:8080", 5000, httpx.MockTransport(simulado.clientes))
    app.dependency_overrides[ruta.estados] = lambda: EstadoPolizas(cliente, clientes, 5000)
    yield simulado
    app.dependency_overrides.clear()


def _get(ruta_: str, encabezados: dict[str, str] = ASESOR, **parametros: Any) -> httpx.Response:
    return TestClient(app).get(ruta_, headers=encabezados, params=parametros)


def _json(estado: int, cuerpo: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(estado, json=cuerpo)


def _lanza(error: type[httpx.HTTPError]) -> Callable[[httpx.Request], httpx.Response]:
    def responder(pedido: httpx.Request) -> httpx.Response:
        raise error("falla de consulta", request=pedido)

    return responder


RUTAS = pytest.mark.parametrize("ruta_", [RUTA_ESTADO, RUTA_LISTADO], ids=["estado", "listado"])


# --- Lo que el portal recibe ------------------------------------------------------------------


def test_el_asesor_recibe_el_estado_y_la_fecha_de_actualizacion(consulta: Simulado) -> None:
    respuesta = _get(RUTA_ESTADO)
    esperado = {k: v for k, v in DETALLE.items() if k != "clienteId"} | TITULAR
    assert (respuesta.status_code, respuesta.json()) == (200, esperado)
    [pedido] = consulta.pedidos
    assert (pedido.method, pedido.url.path) == ("GET", RUTA_ESTADO)
    assert (pedido.headers["X-Actor-Id"], pedido.headers["X-Actor-Rol"]) == ("sub-asesor", "ASESOR")
    assert pedido.headers["X-Correlation-Id"] == "corr-bff-w18"


@pytest.mark.parametrize("estado", ESTADOS)
def test_el_estado_pasa_tal_cual(consulta: Simulado, estado: str) -> None:
    consulta.responder = _json(200, {**DETALLE, "estado": estado})
    assert _get(RUTA_ESTADO).json()["estado"] == estado


def test_un_estado_fuera_del_enum_es_503(consulta: Simulado) -> None:
    consulta.responder = _json(200, {**DETALLE, "estado": "ACTIVA"})
    assert _get(RUTA_ESTADO).status_code == 503


def test_el_listado_sin_filtros_usa_la_paginacion_por_omision(consulta: Simulado) -> None:
    respuesta = _get(RUTA_LISTADO)
    assert (respuesta.status_code, respuesta.json()) == (200, PAGINA)
    [pedido] = consulta.pedidos
    assert (pedido.url.path, dict(pedido.url.params)) == (
        "/v1/polizas",
        {"pagina": "1", "tamano": "20"},
    )
    assert (pedido.headers["X-Actor-Id"], pedido.headers["X-Actor-Rol"]) == ("sub-asesor", "ASESOR")


def test_el_listado_reenvia_cada_filtro_y_la_paginacion(consulta: Simulado) -> None:
    respuesta = _get(
        RUTA_LISTADO,
        numero="100-2026-000001",
        ramo="Vida",
        estado="POR_VENCER",
        vigenciaDesde="2026-11-01",
        pagina=3,
        tamano=50,
    )
    assert respuesta.status_code == 200
    assert dict(consulta.pedidos[0].url.params) == {
        "numero": "100-2026-000001",
        "ramo": "Vida",
        "estado": "POR_VENCER",
        "vigenciaDesde": "2026-11-01",
        "pagina": "3",
        "tamano": "50",
    }


def test_el_listado_vacio_sale_con_actualizado_en_nulo(consulta: Simulado) -> None:
    vacio = {"items": [], "total": 0, "pagina": 1, "tamano": 20, "actualizadoEn": None}
    consulta.responder = _json(200, vacio)
    respuesta = _get(RUTA_LISTADO)
    assert (respuesta.status_code, respuesta.json()) == (200, vacio)


def test_ningun_dato_del_cliente_llega_al_portal(consulta: Simulado) -> None:
    """Si consulta agregara campos (contrato aditivo), el portal recibe solo los del DTO."""
    consulta.responder = lambda pedido: httpx.Response(
        200,
        json=(
            {**DETALLE, "documentoHuella": "h", "nombre": "N"}
            if pedido.url.path.endswith("/estado")
            else {**PAGINA, "items": [{**RESUMEN, "clienteId": "c-1", "documento": "1"}]}
        ),
    )
    detalle, pagina = _get(RUTA_ESTADO).json(), _get(RUTA_LISTADO).json()
    assert detalle == {k: v for k, v in DETALLE.items() if k != "clienteId"} | TITULAR
    assert pagina["items"] == [RESUMEN]


def test_los_parametros_ajenos_se_ignoran(consulta: Simulado) -> None:
    ajenos = {
        "clienteId": "c-1",
        "socioId": "s-1",
        "rol": "SUSCRIPTOR",
        "actor": "otro",
        "X-Actor-Rol": "SUSCRIPTOR",
    }
    encabezados = {**ASESOR, "X-Actor-Id": "otro", "X-Actor-Rol": "SUSCRIPTOR"}
    for ruta_ in (RUTA_ESTADO, RUTA_LISTADO):
        assert _get(ruta_, encabezados, **ajenos).status_code == 200
    estado, listado = consulta.pedidos
    assert dict(estado.url.params) == {}
    assert dict(listado.url.params) == {"pagina": "1", "tamano": "20"}
    for pedido in (estado, listado):
        assert (pedido.headers["X-Actor-Id"], pedido.headers["X-Actor-Rol"]) == (
            "sub-asesor",
            "ASESOR",
        )


# --- Autorización -----------------------------------------------------------------------------


@RUTAS
@pytest.mark.parametrize("grupo", list(OTROS))
def test_otro_grupo_es_403_sin_llamar_a_consulta(
    consulta: Simulado, ruta_: str, grupo: str
) -> None:
    respuesta = _get(ruta_, OTROS[grupo])
    assert respuesta.status_code == 403
    assert respuesta.json()["codigo"] == "ROL_NO_AUTORIZADO"
    assert consulta.pedidos == []


@RUTAS
def test_sin_usuario_autenticado_es_403_sin_llamar_a_consulta(
    consulta: Simulado, ruta_: str
) -> None:
    respuesta = _get(ruta_, {})
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    assert consulta.pedidos == []


def test_sin_sub_es_401_actor_requerido(consulta: Simulado) -> None:
    sin_sub = {k: v for k, v in ASESOR.items() if k != "X-Authenticated-Sub"}
    respuesta = _get(RUTA_ESTADO, sin_sub)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (401, "ACTOR_REQUERIDO")
    assert consulta.pedidos == []


# --- 404 y 422 --------------------------------------------------------------------------------


def test_la_poliza_inexistente_es_404_uniforme(consulta: Simulado) -> None:
    consulta.responder = _json(404, NO_ENCONTRADA)
    respuesta = _get(RUTA_ESTADO)
    assert respuesta.status_code == 404
    cuerpo = respuesta.json()
    assert (cuerpo["codigo"], cuerpo["mensaje"]) == ("POLIZA_NO_ENCONTRADA", "La póliza no existe")
    assert cuerpo["correlationId"]


@pytest.mark.parametrize(
    ("ruta_", "parametros"),
    [
        ("/v1/polizas/no-es-uuid/estado", {}),
        (RUTA_LISTADO, {"numero": "100-2026-1"}),
        (RUTA_LISTADO, {"estado": "ACTIVA"}),
        (RUTA_LISTADO, {"vigenciaDesde": "ayer"}),
        (RUTA_LISTADO, {"pagina": 0}),
        (RUTA_LISTADO, {"tamano": 51}),
        (RUTA_LISTADO, {"tamano": 0}),
        (RUTA_LISTADO, {"ramo": ""}),
        (RUTA_LISTADO, {"ramo": "x" * 81}),
    ],
    ids=[
        "id",
        "numero",
        "estado",
        "vigenciaDesde",
        "pagina",
        "tamano-alto",
        "tamano-cero",
        "ramo-vacio",
        "ramo-largo",
    ],
)
def test_id_o_filtro_invalido_es_422_sin_llamar_a_consulta(
    consulta: Simulado, ruta_: str, parametros: dict[str, Any]
) -> None:
    respuesta = _get(ruta_, **parametros)
    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    # El valor recibido no vuelve en el cuerpo.
    valor = str(next(iter(parametros.values()), "no-es-uuid"))
    if valor not in ("0", "51", "") and len(valor) > 3:
        assert valor not in respuesta.text
    assert consulta.pedidos == []


def test_el_422_de_consulta_pasa(consulta: Simulado) -> None:
    cuerpo = {
        "codigo": "SOLICITUD_INVALIDA",
        "mensaje": "Solicitud inválida",
        "correlationId": "c",
        "detalles": {"campos": ["pagina"]},
    }
    consulta.responder = _json(422, cuerpo)
    respuesta = _get(RUTA_LISTADO)
    assert (respuesta.status_code, respuesta.json()["detalles"]) == (422, {"campos": ["pagina"]})


# --- Falla cerrada: 503 -----------------------------------------------------------------------

FALLOS: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "401": _json(401, {"codigo": "ACTOR_REQUERIDO", "mensaje": "interno", "detalles": {"x": 1}}),
    "403": _json(403, {"codigo": "ROL_NO_AUTORIZADO", "mensaje": "interno", "detalles": {}}),
    "404-otro-codigo": _json(404, {"codigo": "OTRA", "mensaje": "m", "detalles": {}}),
    "422-otro-codigo": _json(422, {"codigo": "OTRA", "mensaje": "m", "detalles": {}}),
    "500": _json(500, {"codigo": "ERROR_INTERNO", "mensaje": "traza", "detalles": {}}),
    "503": _json(503, {"codigo": "DEPENDENCIA_NO_DISPONIBLE", "detalles": {"x": 1}}),
    "sin-cuerpo": lambda _: httpx.Response(502, text="<html>"),
    "no-json": lambda _: httpx.Response(200, text="<html>"),
    "no-objeto": _json(200, []),
    "cuerpo-fuera-de-contrato": _json(200, {"polizaId": POLIZA_ID}),
    "timeout": _lanza(httpx.ReadTimeout),
    "caido": _lanza(httpx.ConnectError),
}


@RUTAS
@pytest.mark.parametrize("fallo", list(FALLOS))
def test_cualquier_falla_de_consulta_es_503_sin_detalles(
    consulta: Simulado, ruta_: str, fallo: str
) -> None:
    consulta.responder = FALLOS[fallo]
    respuesta = _get(ruta_)
    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["codigo"] == "SERVICIO_NO_DISPONIBLE"
    assert cuerpo["detalles"] == {}
    assert "interno" not in respuesta.text and "traza" not in respuesta.text


def test_sin_sesion_ni_datos_en_los_logs(
    consulta: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    consulta.responder = _json(500, {"codigo": "ERROR_INTERNO", "mensaje": "secreto-interno"})
    _get(RUTA_ESTADO)
    assert "secreto-interno" not in caplog.text
    assert "sub-asesor" not in caplog.text


# --- Configuración y cliente ------------------------------------------------------------------


@pytest.mark.parametrize(
    "falla",
    [
        _json(503, {}),
        _json(404, {"codigo": "CLIENTE_NO_ENCONTRADO"}),
        _lanza(httpx.ConnectError),
        _lanza(httpx.ReadTimeout),
        _json(200, ["no es un objeto"]),
    ],
    ids=["5xx", "404", "caida", "timeout", "cuerpo-invalido"],
)
def test_si_clientes_falla_la_poliza_responde_con_el_titular_nulo(
    consulta: Simulado, falla: Callable[[httpx.Request], httpx.Response]
) -> None:
    consulta.responder_clientes = falla
    respuesta = _get(RUTA_ESTADO)
    assert respuesta.status_code == 200
    assert {k: respuesta.json()[k] for k in TITULAR} == SIN_TITULAR


def test_un_documento_sin_enmascarar_se_descarta(consulta: Simulado) -> None:
    consulta.responder_clientes = _json(
        200, {"nombreCompleto": "Natalia", "tipoDocumento": "CC", "documentoEnmascarado": "1020456"}
    )
    cuerpo = _get(RUTA_ESTADO).json()
    assert cuerpo["titularNombre"] == "Natalia"
    assert cuerpo["documentoEnmascarado"] is None
    assert "1020456" not in str(cuerpo)


def test_el_cliente_id_no_llega_al_portal_y_clientes_se_consulta_por_el_dueno(
    consulta: Simulado,
) -> None:
    cuerpo = _get(RUTA_ESTADO).json()
    assert "clienteId" not in cuerpo
    [pedido] = consulta.pedidos_clientes
    assert CLIENTE_ID in pedido.url.path


def test_el_titular_no_aparece_en_los_logs(
    consulta: Simulado, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _get(RUTA_ESTADO)
    assert "Natalia" not in caplog.text
    assert "1.020.***.456" not in caplog.text


def test_la_poliza_ajena_o_inexistente_sigue_en_404_sin_llamar_a_clientes(
    consulta: Simulado,
) -> None:
    consulta.responder = _json(404, NO_ENCONTRADA)
    assert _get(RUTA_ESTADO).status_code == 404
    assert consulta.pedidos_clientes == []


# --- Configuración y cliente (cont.) ----------------------------------------------------------


def test_consulta_tiene_corte_duro_de_700_ms() -> None:
    assert settings.consulta_timeout_ms == 700
    assert ConsultaCliente(settings.consulta_url, settings.consulta_timeout_ms).timeout == 0.7


def test_el_servicio_por_omision_se_arma_con_la_configuracion() -> None:
    ruta.estados.cache_clear()
    servicio = ruta.estados()
    assert servicio.consulta.url == settings.consulta_url.rstrip("/")
    assert servicio.consulta.timeout == settings.consulta_timeout_ms / 1000
    ruta.estados.cache_clear()
