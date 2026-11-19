"""HU-W22 (bff-web): H09 «Siniestros paramétricos» con `pagos-parametricos`, `siniestros` y CloudWatch
simulados.

La fuente principal (`pagos-parametricos`) falla cerrada (503 sin detalles); las secundarias
(CloudWatch y el estado de pago de `siniestros`) dejan su campo en `null` y **nunca** dan 503. Solo
`analistas-siniestros`; el actor y la correlación viajan a cada servicio.
"""

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from bff_web.adapters.inbound import parametricos as ruta
from bff_web.adapters.outbound.cloudwatch import MetricasParametricos
from bff_web.adapters.outbound.pagos_parametricos import PagosParametricosCliente
from bff_web.adapters.outbound.siniestros import SiniestrosCliente
from bff_web.application.parametricos import Parametricos
from bff_web.application.siniestros import esperar_avisos
from bff_web.config import settings
from bff_web.main import app
from fastapi.testclient import TestClient

BACKOFFICE = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_back"
RESUMEN = "/v1/siniestros/parametricos/resumen"
ORIGENES = "/v1/siniestros/parametricos/eventos-origen"
SINIESTRO = "5a000000-0000-4000-8000-000000000022"
OTRO = "5a000000-0000-4000-8000-000000000023"
POLIZA = "b2100000-0000-4000-8000-000000000101"
DISPARADOR = {
    "tipoEvento": "LLUVIA_ACUMULADA",
    "valorObservado": "135",
    "umbral": "120.000",
    "unidad": "mm",
}


def _fila(siniestro: str = SINIESTRO, codigo: str = "88-2026-1001") -> dict[str, Any]:
    return {
        "siniestroId": siniestro,
        "siniestroCodigo": codigo,
        "polizaId": POLIZA,
        "polizaNumero": "250-2026-000101",
        "disparador": DISPARADOR,
        "valor": "1400000.00",
        "moneda": "COP",
        "estadoActivacion": "SINIESTRO_CREADO",
    }


def _resumen_pp(**cambios: Any) -> dict[str, Any]:
    minutos = [
        {"minuto": f"2026-11-10T14:{m:02d}:00Z", "procesados": 1 if m == 7 else 0}
        for m in range(15)
    ]
    cuerpo: dict[str, Any] = {
        "eventoOrigen": "EVT-2026-0913",
        "desde": "2026-11-10T14:01:00Z",
        "recibidos": 94,
        "procesados": 90,
        "activados": 12,
        "duplicados": 4,
        "enDlq": 3,
        "ritmoPorMinuto": minutos,
        "ultimosPagos": [_fila(), _fila(OTRO, "88-2026-1002")],
    }
    return cuerpo | cambios


def _usuario(grupos: str, sub: str = "sub-analista") -> dict[str, str]:
    return {
        "X-Authenticated-Groups": grupos,
        "X-Authenticated-Issuer": BACKOFFICE,
        "X-Authenticated-Sub": sub,
        "X-Correlation-Id": "corr-bff-w22",
    }


ANALISTA = _usuario("[analistas-siniestros]")


class Simulado:
    """Un servicio simulado: guarda los pedidos y responde lo que se le diga."""

    def __init__(self, respuesta: Callable[[httpx.Request], httpx.Response]) -> None:
        self.pedidos: list[httpx.Request] = []
        self.responder = respuesta

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        self.pedidos.append(pedido)
        return self.responder(pedido)


class MetricasFalsas:
    def __init__(self, resultado: dict[str, int | None] | Exception) -> None:
        self.resultado = resultado
        self.llamadas: list[tuple[Any, ...]] = []

    def leer(
        self, cola: str, dlq: str, cluster: str | None, servicio: str | None
    ) -> dict[str, int | None]:
        self.llamadas.append((cola, dlq, cluster, servicio))
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return self.resultado


class Entorno:
    def __init__(self) -> None:
        self.pp = Simulado(lambda _: httpx.Response(200, json=_resumen_pp()))
        self.sin = Simulado(
            lambda _: httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "siniestroId": SINIESTRO,
                            "codigo": "88-2026-1001",
                            "estadoPago": "CONFIRMADO",
                        },
                        {"siniestroId": OTRO, "codigo": "88-2026-1002", "estadoPago": "PENDIENTE"},
                    ]
                },
            )
        )
        self.metricas = MetricasFalsas({"profundidad": 576, "dlq": 3, "tareas": 24})
        self.maximos: int | None = 30

    def servicio(self, con_metricas: bool = True) -> Parametricos:
        return Parametricos(
            PagosParametricosCliente(
                "http://pagos-parametricos:8080", 3000, httpx.MockTransport(self.pp)
            ),
            SiniestrosCliente("http://siniestros:8080", 1500, httpx.MockTransport(self.sin)),
            (lambda: self.metricas) if con_metricas else None,
            "solventa-local-parametricos-eventos",
            "solventa-local",
            "solventa-local-pagos-parametricos",
            self.maximos,
            700,
        )


@pytest.fixture
def entorno(monkeypatch: pytest.MonkeyPatch) -> Iterator[Entorno]:
    e = Entorno()
    monkeypatch.setattr(settings, "jwt_issuers", {"backoffice": BACKOFFICE})
    app.dependency_overrides[ruta.parametricos_servicio] = lambda: e.servicio()
    yield e
    app.dependency_overrides.clear()


def _pedir(**parametros: Any) -> Any:
    return TestClient(app).get(RESUMEN, params=parametros, headers=ANALISTA)


def test_h09_compone_el_resumen_la_cola_y_el_estado_de_pago_de_cada_fila(entorno: Entorno) -> None:
    respuesta = _pedir(eventoOrigen="EVT-2026-0913")

    assert respuesta.status_code == 200
    c = respuesta.json()
    assert (c["eventoOrigen"], c["recibidos"], c["procesados"], c["activados"], c["duplicados"]) == (
        "EVT-2026-0913", 94, 90, 12, 4,
    )  # fmt: skip
    assert c["cola"] == {"profundidad": 576, "consumidoresActivos": 24, "consumidoresMaximos": 30}
    assert c["enDlq"] == 3 and c["p95VentaConsultaMs"] is None
    assert [(f["siniestroCodigo"], f["estadoPago"]) for f in c["ultimosPagos"]] == [
        ("88-2026-1001", "CONFIRMADO"),
        ("88-2026-1002", "PENDIENTE"),
    ]
    assert len(c["ritmoPorMinuto"]) == 15
    assert entorno.metricas.llamadas == [
        (
            "solventa-local-parametricos-eventos",
            "solventa-local-parametricos-eventos-dlq",
            "solventa-local",
            "solventa-local-pagos-parametricos",
        )
    ]


def test_el_actor_y_la_correlacion_viajan_a_pagos_parametricos_y_a_siniestros(
    entorno: Entorno,
) -> None:
    _pedir(eventoOrigen="EVT-2026-0913")

    (pp,) = entorno.pp.pedidos
    assert (pp.url.path, dict(pp.url.params)) == (
        "/v1/parametricos/resumen",
        {"eventoOrigen": "EVT-2026-0913"},
    )
    assert (pp.headers["X-Actor-Id"], pp.headers["X-Actor-Rol"]) == (
        "sub-analista",
        "ANALISTA_SINIESTROS",
    )
    assert pp.headers["X-Correlation-Id"] == "corr-bff-w22"
    (sin,) = entorno.sin.pedidos
    assert sin.url.path == "/v1/siniestros/parametricos/pagos"
    # El parámetro se repite, una vez por fila.
    assert sin.url.params.get_list("siniestroIds") == [SINIESTRO, OTRO]
    assert (
        sin.headers["X-Actor-Rol"] == "ANALISTA_SINIESTROS"
        and sin.headers["X-Actor-Id"] == "sub-analista"
    )
    assert sin.headers["X-Correlation-Id"] == "corr-bff-w22"


def test_sin_origen_no_envia_el_parametro_y_pagos_parametricos_elige_el_mas_reciente(
    entorno: Entorno,
) -> None:
    _pedir()

    assert dict(entorno.pp.pedidos[0].url.params) == {}


def test_si_siniestros_no_responde_el_estado_de_pago_va_en_null_y_no_hay_503(
    entorno: Entorno,
) -> None:
    entorno.sin.responder = lambda _: httpx.Response(
        503, json={"codigo": "DEPENDENCIA_NO_DISPONIBLE"}
    )

    respuesta = _pedir()

    assert respuesta.status_code == 200
    assert [f["estadoPago"] for f in respuesta.json()["ultimosPagos"]] == [None, None]
    assert respuesta.json()["procesados"] == 90  # lo principal se conserva


@pytest.mark.parametrize(
    "falla",
    [
        lambda _: httpx.Response(200, json={"items": "no-es-una-lista"}),
        lambda _: httpx.Response(200, text="no es json"),
        lambda _: httpx.Response(403, json={"codigo": "ROL_NO_AUTORIZADO"}),
    ],
)
def test_una_respuesta_rara_de_siniestros_tambien_deja_el_estado_en_null(
    entorno: Entorno, falla: Callable[[httpx.Request], httpx.Response]
) -> None:
    entorno.sin.responder = falla

    assert [f["estadoPago"] for f in _pedir().json()["ultimosPagos"]] == [None, None]


def test_un_timeout_de_siniestros_es_un_null_no_un_503(entorno: Entorno) -> None:
    def caida(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("lento")

    entorno.sin.responder = caida

    assert _pedir().status_code == 200


def test_un_id_de_pago_que_no_es_uuid_no_llama_a_siniestros(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(
        200, json=_resumen_pp(ultimosPagos=[{**_fila(), "siniestroId": "no-es-uuid"}])
    )

    respuesta = _pedir()

    # El DTO rechaza el id malformado: es una respuesta fuera de contrato (503) y siniestros no se llamó.
    assert respuesta.status_code == 503 and entorno.sin.pedidos == []


def test_si_cloudwatch_falla_la_cola_va_en_null_y_la_dlq_queda_la_de_pagos_parametricos(
    entorno: Entorno,
) -> None:
    entorno.metricas.resultado = PermissionError("sin cloudwatch:GetMetricData")

    c = _pedir().json()

    assert c["cola"] == {
        "profundidad": None,
        "consumidoresActivos": None,
        "consumidoresMaximos": 30,
    }
    assert c["enDlq"] == 3


def test_si_pagos_parametricos_no_lee_la_dlq_se_usa_la_de_cloudwatch(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(200, json=_resumen_pp(enDlq=None))
    entorno.metricas.resultado = {"profundidad": 10, "dlq": 2, "tareas": None}

    c = _pedir().json()

    assert c["enDlq"] == 2 and c["cola"]["consumidoresActivos"] is None


def test_si_ninguna_fuente_da_la_dlq_va_en_null(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(200, json=_resumen_pp(enDlq=None))
    entorno.metricas.resultado = TimeoutError()

    assert _pedir().json()["enDlq"] is None


def test_sin_metricas_configuradas_la_cola_va_en_null(entorno: Entorno) -> None:
    app.dependency_overrides[ruta.parametricos_servicio] = lambda: entorno.servicio(
        con_metricas=False
    )
    entorno.maximos = None

    c = _pedir().json()

    assert c["cola"] == {
        "profundidad": None,
        "consumidoresActivos": None,
        "consumidoresMaximos": None,
    }
    assert c["ultimosPagos"][0]["estadoPago"] == "CONFIRMADO"


def test_sin_ultimos_pagos_no_llama_a_siniestros(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(200, json=_resumen_pp(ultimosPagos=[]))

    c = _pedir().json()

    assert c["ultimosPagos"] == [] and entorno.sin.pedidos == []


def test_un_origen_sin_eventos_responde_en_cero(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(
        200,
        json=_resumen_pp(
            eventoOrigen=None, desde=None, recibidos=0, procesados=0, activados=0, duplicados=0,
            ultimosPagos=[],
        ),
    )  # fmt: skip

    c = _pedir().json()

    assert (c["eventoOrigen"], c["desde"], c["procesados"]) == (None, None, 0)


# --- La fuente principal falla cerrada ------------------------------------------------------------
@pytest.mark.parametrize(
    "falla",
    [
        lambda _: httpx.Response(503, json={"codigo": "DEPENDENCIA_NO_DISPONIBLE"}),
        lambda _: httpx.Response(500),
        lambda _: httpx.Response(401, json={"codigo": "ACTOR_REQUERIDO"}),
        lambda _: httpx.Response(403, json={"codigo": "ROL_NO_AUTORIZADO"}),
        lambda _: httpx.Response(404, json={"codigo": "OTRO"}),
        lambda _: httpx.Response(422, json={"codigo": "OTRO_CODIGO"}),
        lambda _: httpx.Response(200, text="no es json"),
        lambda _: httpx.Response(200, json=["no", "es", "un", "objeto"]),
        lambda _: httpx.Response(200, json={"eventoOrigen": "EVT"}),  # le faltan campos
    ],
)
def test_si_pagos_parametricos_no_responde_o_responde_fuera_de_contrato_es_503_sin_detalles(
    entorno: Entorno, falla: Callable[[httpx.Request], httpx.Response]
) -> None:
    entorno.pp.responder = falla

    respuesta = _pedir()

    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert (cuerpo["codigo"], cuerpo["detalles"]) == ("SERVICIO_NO_DISPONIBLE", {})


def test_una_conexion_caida_de_pagos_parametricos_es_503(entorno: Entorno) -> None:
    def caida(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta")

    entorno.pp.responder = caida

    assert _pedir().status_code == 503


def test_el_422_de_pagos_parametricos_pasa_con_su_codigo(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(
        422,
        json={
            "codigo": "SOLICITUD_INVALIDA",
            "mensaje": "x",
            "detalles": {"campos": ["eventoOrigen"]},
        },
    )

    respuesta = _pedir()

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (422, "SOLICITUD_INVALIDA")
    assert respuesta.json()["detalles"] == {"campos": ["eventoOrigen"]}


def test_un_origen_con_formato_invalido_es_422_sin_llamar_a_nadie(entorno: Entorno) -> None:
    respuesta = _pedir(eventoOrigen="con espacios")

    assert respuesta.status_code == 422 and entorno.pp.pedidos == []


# --- Orígenes recientes ----------------------------------------------------------------------------
ORIGENES_PP = {
    "items": [
        {
            "eventoOrigen": "EVT-2026-0913",
            "desde": "2026-11-10T14:01:00Z",
            "hasta": "2026-11-10T14:07:00Z",
            "eventos": 90,
        }
    ]
}


def test_los_origenes_recientes_pasan_tal_cual(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(200, json=ORIGENES_PP)

    respuesta = TestClient(app).get(ORIGENES, params={"limite": 5}, headers=ANALISTA)

    assert (respuesta.status_code, respuesta.json()) == (200, ORIGENES_PP)
    (pp,) = entorno.pp.pedidos
    assert (pp.url.path, dict(pp.url.params)) == (
        "/v1/parametricos/eventos-origen",
        {"limite": "5"},
    )


@pytest.mark.parametrize("limite", [0, 51])
def test_el_limite_de_los_origenes_va_de_1_a_50(entorno: Entorno, limite: int) -> None:
    assert (
        TestClient(app).get(ORIGENES, params={"limite": limite}, headers=ANALISTA).status_code
        == 422
    )


def test_origenes_fuera_de_contrato_o_caidos_son_503(entorno: Entorno) -> None:
    entorno.pp.responder = lambda _: httpx.Response(200, json={"items": [{"eventoOrigen": "X"}]})
    assert TestClient(app).get(ORIGENES, headers=ANALISTA).status_code == 503
    entorno.pp.responder = lambda _: httpx.Response(500)
    assert TestClient(app).get(ORIGENES, headers=ANALISTA).status_code == 503
    entorno.pp.responder = lambda _: httpx.Response(
        422, json={"codigo": "SOLICITUD_INVALIDA", "mensaje": "x"}
    )
    assert TestClient(app).get(ORIGENES, headers=ANALISTA).status_code == 422


# --- Seguridad ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("ruta_", [RESUMEN, ORIGENES])
@pytest.mark.parametrize("grupos", ["[asesores]", "[operacion]", "[]"])
def test_solo_el_grupo_analistas_siniestros_y_se_avisa_a_siniestros(
    entorno: Entorno, ruta_: str, grupos: str
) -> None:
    respuesta = TestClient(app).get(ruta_, headers=_usuario(grupos))

    assert (respuesta.status_code, respuesta.json()["codigo"]) == (403, "ROL_NO_AUTORIZADO")
    assert entorno.pp.pedidos == []


@pytest.mark.parametrize("ruta_", [RESUMEN, ORIGENES])
def test_sin_usuario_es_401(entorno: Entorno, ruta_: str) -> None:
    assert TestClient(app).get(ruta_).status_code in (401, 403)


def test_el_aviso_de_acceso_denegado_es_del_recurso_resumen(entorno: Entorno) -> None:
    TestClient(app).get(RESUMEN, headers=_usuario("[asesores]"))
    asyncio.run(esperar_avisos())


# --- Rutas --------------------------------------------------------------------------------------------
def test_las_rutas_de_h09_van_antes_que_el_detalle_del_siniestro() -> None:
    rutas = list(app.openapi()["paths"])

    assert rutas.index(RESUMEN) < rutas.index("/v1/siniestros/{siniestroId}")
    assert rutas.index(ORIGENES) < rutas.index("/v1/siniestros/{siniestroId}")


# --- CloudWatch ----------------------------------------------------------------------------------------
class ClienteCloudWatch:
    def __init__(self, resultados: list[dict[str, Any]]) -> None:
        self.resultados = resultados
        self.llamadas: list[dict[str, Any]] = []

    def get_metric_data(self, **argumentos: Any) -> dict[str, Any]:
        self.llamadas.append(argumentos)
        return {"MetricDataResults": self.resultados}


AHORA = datetime(2026, 11, 10, 14, 10, tzinfo=UTC)


def test_cloudwatch_lee_cola_dlq_y_tareas_en_una_sola_consulta_y_toma_el_ultimo_valor() -> None:
    cliente = ClienteCloudWatch(
        [
            {
                "Id": "profundidad",
                "Timestamps": [AHORA.replace(minute=8), AHORA.replace(minute=9)],
                "Values": [400.0, 576.0],
            },
            {"Id": "dlq", "Timestamps": [AHORA], "Values": [3.0]},
            {
                "Id": "tareas",
                "Timestamps": [AHORA.replace(minute=9), AHORA.replace(minute=8)],
                "Values": [24.0, 22.0],
            },
        ]
    )
    metricas = MetricasParametricos(cliente, lambda: AHORA)

    leidas = metricas.leer("cola", "cola-dlq", "cluster", "servicio")

    assert leidas == {"profundidad": 576, "dlq": 3, "tareas": 24}
    (llamada,) = cliente.llamadas
    consultas = {c["Id"]: c["MetricStat"] for c in llamada["MetricDataQueries"]}
    assert consultas["profundidad"]["Metric"]["Namespace"] == "AWS/SQS"
    assert consultas["profundidad"]["Metric"]["MetricName"] == "ApproximateNumberOfMessagesVisible"
    assert consultas["profundidad"]["Metric"]["Dimensions"] == [
        {"Name": "QueueName", "Value": "cola"}
    ]
    assert consultas["dlq"]["Metric"]["Dimensions"] == [{"Name": "QueueName", "Value": "cola-dlq"}]
    assert consultas["tareas"]["Metric"]["MetricName"] == "RunningTaskCount"
    assert consultas["tareas"]["Metric"]["Dimensions"] == [
        {"Name": "ClusterName", "Value": "cluster"},
        {"Name": "ServiceName", "Value": "servicio"},
    ]
    assert llamada["EndTime"] == AHORA and llamada["StartTime"] < AHORA


def test_cloudwatch_sin_cluster_o_servicio_no_pregunta_por_las_tareas() -> None:
    cliente = ClienteCloudWatch([{"Id": "profundidad", "Timestamps": [AHORA], "Values": [5.0]}])

    leidas = MetricasParametricos(cliente, lambda: AHORA).leer("cola", "cola-dlq", None, "servicio")

    assert leidas == {"profundidad": 5, "dlq": None, "tareas": None}
    assert [c["Id"] for c in cliente.llamadas[0]["MetricDataQueries"]] == ["profundidad", "dlq"]


def test_una_serie_sin_puntos_es_null() -> None:
    cliente = ClienteCloudWatch([{"Id": "profundidad", "Timestamps": [], "Values": []}])

    assert MetricasParametricos(cliente).leer("c", "d", None, None)["profundidad"] is None


def test_el_cliente_de_cloudwatch_se_crea_sin_reintentos_y_con_corte_corto() -> None:
    from bff_web.adapters.outbound.cloudwatch import crear_metricas_parametricos

    metricas = crear_metricas_parametricos("us-east-1")

    assert metricas.cliente.meta.config.read_timeout == 1


def test_el_servicio_se_arma_con_los_nombres_de_la_infraestructura_por_omision() -> None:
    ruta.parametricos_servicio.cache_clear()
    try:
        servicio = ruta.parametricos_servicio()
    finally:
        ruta.parametricos_servicio.cache_clear()

    assert isinstance(servicio, Parametricos)
    assert servicio._cola == f"solventa-{settings.environment}-parametricos-eventos"  # type: ignore[attr-defined]
    assert isinstance(ruta._metricas(), MetricasParametricos)
