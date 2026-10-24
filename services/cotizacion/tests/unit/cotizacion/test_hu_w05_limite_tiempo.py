"""N1 de HU-W05 en cotizacion: presupuesto del recorrido, validación al arrancar, X-Deadline-Ms
hacia catalogo y clientes, reintento acotado y el punto de extensión de HU-W06."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from cotizacion.adapters.outbound.clientes_http import ClientesHttp
from cotizacion.adapters.outbound.productos_catalogo import ProductosCatalogo
from cotizacion.application.cotizacion.calcular_prima import (
    DEPENDENCIA_CLIENTE,
    DEPENDENCIA_PRODUCTO,
    CalcularPrima,
    ConfigCotizacion,
    FallaCerrada,
    ResultadoDependencia,
)
from cotizacion.config import PRESUPUESTO_RECORRIDO_MS, Settings
from cotizacion.domain.perfilamiento.errores import DependenciaAgotada, DependenciaNoDisponible
from cotizacion.main import Puertos, crear_app
from fastapi.testclient import TestClient
from pydantic import ValidationError
from solventa_common import presupuesto
from solventa_common.resiliencia import ProteccionDependencia

from .test_api import ASESOR, C01, clave
from .test_calcular_prima import PERFIL_C01
from .test_productos_catalogo import AHORA, Catalogo, Reloj

pytestmark = pytest.mark.anyio
TOLERANCIA_MS = 60
CLIENTE = "00000000-0000-4000-8000-000000000001"


# --- Configuración por ambiente y validación al arrancar -------------------------------------


def test_sin_latency_budget_usa_el_valor_por_omision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LATENCY_BUDGET_MS", raising=False)
    assert Settings().presupuesto_recorrido_ms == PRESUPUESTO_RECORRIDO_MS == 700


def test_lee_el_presupuesto_de_latency_budget_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lo que entrega infra/apps/ecs.tf con latency_budget_ms de int y el override de DEF-03.
    monkeypatch.setenv("LATENCY_BUDGET_MS", '{"cotizacion":700,"socio.cotizar":1000}')
    monkeypatch.setenv("SOLVENTA_DEPENDENCIA_TIMEOUT_MS", "300")
    assert Settings().presupuesto_recorrido_ms == 700


@pytest.mark.parametrize(("presupuesto_ms", "timeout_ms"), [(300, 300), (250, 300)])
def test_un_timeout_que_no_es_menor_que_el_presupuesto_no_arranca(
    monkeypatch: pytest.MonkeyPatch, presupuesto_ms: int, timeout_ms: int
) -> None:
    monkeypatch.setenv("LATENCY_BUDGET_MS", f'{{"cotizacion":{presupuesto_ms}}}')
    monkeypatch.setenv("SOLVENTA_DEPENDENCIA_TIMEOUT_MS", str(timeout_ms))
    monkeypatch.setenv("SOLVENTA_DB_PASSWORD", "no-debe-aparecer")
    with pytest.raises(ValidationError) as error:
        Settings()
    assert "dependencia_timeout_ms" in str(error.value)
    assert "no-debe-aparecer" not in str(error.value)


def test_el_pool_debe_cubrir_los_bulkheads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_BULKHEAD_MAX", "100")
    monkeypatch.setenv("SOLVENTA_HTTP_MAX_CONEXIONES", "150")
    with pytest.raises(ValidationError, match="bulkheads"):
        Settings()


# --- El recorrido: presupuesto, deadline recibido y resultado TIEMPO_AGOTADO ----------------


def _caso(escenario: Any, **config: Any) -> CalcularPrima:
    caso = escenario.caso()
    caso.config = ConfigCotizacion(**{"dependencia_timeout_ms": 150, **config})
    return caso  # type: ignore[no-any-return]


async def test_ac3_dependencias_a_tiempo_se_usan_normalmente(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    escenario_cotizacion.productos.demora_s = 0.05
    cotizacion, _, creada = await _caso(escenario_cotizacion).ejecutar(nueva_cotizacion())
    assert creada and cotizacion.oferta is not None


async def test_ac1_dependencia_lenta_se_abandona_en_su_timeout(
    escenario_cotizacion: Any, nueva_cotizacion: Any, caplog: pytest.LogCaptureFixture
) -> None:
    escenario_cotizacion.productos.demora_s = 2
    inicio = time.perf_counter()
    with caplog.at_level(logging.INFO), pytest.raises(DependenciaAgotada) as error:
        await _caso(escenario_cotizacion, dependencia_timeout_ms=100).ejecutar(nueva_cotizacion())
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert duracion_ms <= 100 + 10 + TOLERANCIA_MS, duracion_ms
    assert error.value.dependencia == DEPENDENCIA_PRODUCTO
    assert escenario_cotizacion.repo.cotizaciones == {}
    agotados = [r for r in caplog.records if r.message == "dependencia agotó su tiempo"]
    assert [(r.proveedor, r.operacion) for r in agotados] == [  # type: ignore[attr-defined]
        ("catalogo", DEPENDENCIA_PRODUCTO)
    ]
    resultados = {r.getMessage() for r in caplog.records if r.name == "solventa.telemetria"}
    assert f"operación cotizacion.dependencia.{DEPENDENCIA_PRODUCTO}: timeout" in resultados
    assert "operación cotizacion: timeout" in resultados


async def test_el_deadline_recibido_acota_el_recorrido(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    escenario_cotizacion.productos.demora_s = 2
    inicio = time.perf_counter()
    with pytest.raises(DependenciaAgotada):
        await _caso(escenario_cotizacion, dependencia_timeout_ms=600).ejecutar(
            nueva_cotizacion(deadline_ms=80)
        )
    assert (time.perf_counter() - inicio) * 1000 <= 80 + TOLERANCIA_MS


async def test_el_presupuesto_propio_acota_el_recorrido(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    escenario_cotizacion.productos.demora_s = 2
    inicio = time.perf_counter()
    with pytest.raises(DependenciaAgotada):
        await _caso(
            escenario_cotizacion, dependencia_timeout_ms=600, presupuesto_recorrido_ms=120
        ).ejecutar(nueva_cotizacion())
    assert (time.perf_counter() - inicio) * 1000 <= 120 + TOLERANCIA_MS


async def test_dependencia_agotada_es_dependencia_no_disponible() -> None:
    # La ruta la traduce igual: 503 DEPENDENCIA_NO_DISPONIBLE, recuperable.
    assert issubclass(DependenciaAgotada, DependenciaNoDisponible)


class PoliticaEspia:
    """Una política de HU-W06 de prueba: anota lo que recibe y falla cerrada."""

    def __init__(self) -> None:
        self.recibido: list[tuple[str, ResultadoDependencia]] = []

    def ante_falla(self, dependencia: str, resultado: ResultadoDependencia, error: Any) -> Any:
        self.recibido.append((dependencia, resultado))
        FallaCerrada().ante_falla(dependencia, resultado, error)


@pytest.mark.parametrize(
    ("preparar", "esperado"),
    [
        (
            lambda e: setattr(e.productos, "demora_s", 2),
            (DEPENDENCIA_PRODUCTO, ResultadoDependencia.TIEMPO_AGOTADO),
        ),
        (
            lambda e: setattr(e.identidad, "respuesta", DependenciaNoDisponible("clientes")),
            (DEPENDENCIA_CLIENTE, ResultadoDependencia.NO_DISPONIBLE),
        ),
    ],
    ids=["tiempo_agotado", "no_disponible"],
)
async def test_la_politica_de_hu_w06_recibe_el_resultado_de_la_dependencia(
    escenario_cotizacion: Any, nueva_cotizacion: Any, preparar: Any, esperado: Any
) -> None:
    preparar(escenario_cotizacion)
    politica = PoliticaEspia()
    caso = CalcularPrima(
        productos=escenario_cotizacion.productos,
        identidad=escenario_cotizacion.identidad,
        perfiles=escenario_cotizacion.perfiles,
        repo=escenario_cotizacion.repo,
        reloj=escenario_cotizacion.reloj,
        config=ConfigCotizacion(dependencia_timeout_ms=60),
        politica=politica,
    )
    with pytest.raises(DependenciaNoDisponible):
        await caso.ejecutar(nueva_cotizacion())
    assert politica.recibido == [esperado]


async def test_el_perfil_lento_tambien_se_corta(
    escenario_cotizacion: Any, nueva_cotizacion: Any
) -> None:
    async def lento(_: Any) -> Any:
        await asyncio.sleep(2)

    escenario_cotizacion.perfiles.por_id = lento
    with pytest.raises(DependenciaAgotada) as error:
        await _caso(escenario_cotizacion, dependencia_timeout_ms=80).ejecutar(
            nueva_cotizacion(perfil_id=PERFIL_C01)
        )
    assert error.value.dependencia == "perfil"


# --- Propagación: X-Deadline-Ms hacia catalogo y clientes ------------------------------------


Http = Callable[[Any], httpx.AsyncClient]


@pytest.fixture
async def http() -> AsyncIterator[Http]:
    """Clientes httpx sobre MockTransport que se cierran al terminar la prueba."""
    abiertos: list[httpx.AsyncClient] = []

    def crear(manejador: Any) -> httpx.AsyncClient:
        abiertos.append(httpx.AsyncClient(transport=httpx.MockTransport(manejador)))
        return abiertos[-1]

    yield crear
    for cliente in abiertos:
        await cliente.aclose()


def _catalogo(http: Http, catalogo: Catalogo, **opciones: Any) -> ProductosCatalogo:
    return ProductosCatalogo(
        http(catalogo), "http://catalogo:8080", 300, Reloj(), ttl_s=0, **opciones
    )


def _deadline(solicitud: httpx.Request) -> int:
    return int(solicitud.headers["X-Deadline-Ms"])


async def test_sin_recorrido_envia_el_timeout_configurado(http: Http) -> None:
    catalogo = Catalogo()
    await _catalogo(http, catalogo).producto("vida-hipotecario")
    assert _deadline(catalogo.solicitudes[0]) == 300


async def test_en_el_recorrido_envia_el_restante_menos_el_margen(http: Http) -> None:
    catalogo = Catalogo()
    with presupuesto.iniciar(200):
        await _catalogo(http, catalogo).producto("vida-hipotecario")
    assert 150 <= _deadline(catalogo.solicitudes[0]) <= 180


async def test_catalogo_5xx_reintenta_una_vez_con_la_proteccion(http: Http) -> None:
    catalogo = Catalogo()
    respuestas = [httpx.Response(503), None]

    def responder(solicitud: httpx.Request) -> httpx.Response:
        respuesta = respuestas.pop(0)
        return respuesta or Catalogo()(solicitud)

    catalogo_fn: Any = lambda s: (catalogo.solicitudes.append(s), responder(s))[1]
    cliente = http(catalogo_fn)
    productos = ProductosCatalogo(
        cliente, "http://catalogo:8080", 300, Reloj(), ttl_s=0,
        proteccion=ProteccionDependencia(5, 30, 10),
    )  # fmt: skip
    with presupuesto.iniciar(700):
        resuelto = await productos.producto("vida-hipotecario")
    assert resuelto.valor.id == "vida-hipotecario"
    assert len(catalogo.solicitudes) == 2
    # El reintento envía un deadline menor: la misma ventana.
    assert _deadline(catalogo.solicitudes[1]) < _deadline(catalogo.solicitudes[0])


async def test_catalogo_504_es_tiempo_agotado_y_no_se_reintenta(http: Http) -> None:
    catalogo = Catalogo()
    catalogo.respuestas["/vida-hipotecario"] = httpx.Response(
        504, json={"codigo": "TIEMPO_AGOTADO"}
    )
    productos = _catalogo(http, catalogo, proteccion=ProteccionDependencia(5, 30, 10))
    with pytest.raises(DependenciaAgotada):
        await productos.producto("vida-hipotecario")
    assert len(catalogo.solicitudes) == 1


async def test_catalogo_lento_se_abandona_en_el_timeout_efectivo(http: Http) -> None:
    async def lento(solicitud: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200)  # pragma: no cover

    productos = ProductosCatalogo(http(lento), "http://catalogo:8080", 300, Reloj(), ttl_s=0)
    inicio = time.perf_counter()
    with presupuesto.iniciar(150), pytest.raises(DependenciaAgotada):
        await productos.reglas_vigentes("vida-hipotecario", AHORA)
    assert (time.perf_counter() - inicio) * 1000 <= 130 + TOLERANCIA_MS


class Clientes:
    def __init__(self, *respuestas: httpx.Response) -> None:
        self.respuestas = list(respuestas)
        self.solicitudes: list[httpx.Request] = []

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        self.solicitudes.append(solicitud)
        return self.respuestas.pop(0)


def _clientes(http: Http, transporte: Clientes) -> ClientesHttp:
    return ClientesHttp(http(transporte), "http://clientes", 300, ProteccionDependencia(5, 30, 10))


async def test_identidad_envia_el_deadline_y_reintenta_un_5xx(http: Http) -> None:
    identidad = {"clienteId": CLIENTE, "estado": "VERIFICADO", "verificadoEn": None}
    transporte = Clientes(httpx.Response(502), httpx.Response(200, json=identidad))
    with presupuesto.iniciar(700):
        await _clientes(http, transporte).estado(CLIENTE)  # type: ignore[arg-type]
    assert len(transporte.solicitudes) == 2
    assert all("X-Deadline-Ms" in s.headers for s in transporte.solicitudes)


async def test_la_verificacion_de_consentimiento_no_se_reintenta(http: Http) -> None:
    transporte = Clientes(httpx.Response(503))
    with pytest.raises(DependenciaNoDisponible):
        await _clientes(http, transporte).verificar(CLIENTE, "PERFILAMIENTO_PRECIO", [])  # type: ignore[arg-type]
    assert len(transporte.solicitudes) == 1
    assert "X-Deadline-Ms" in transporte.solicitudes[0].headers


# --- La ruta recibe X-Deadline-Ms ------------------------------------------------------------


@pytest.fixture
def api_lenta(escenario: Any, escenario_cotizacion: Any) -> Any:
    escenario_cotizacion.productos.demora_s = 2
    puertos = Puertos(
        identidad=escenario_cotizacion.identidad,
        consentimiento=escenario.consentimiento,
        modelos=escenario.modelos,
        ejecutor=escenario.ejecutor,
        perfiles=escenario_cotizacion.perfiles,
        reloj=escenario_cotizacion.reloj,
        productos=escenario_cotizacion.productos,
        cotizaciones=escenario_cotizacion.repo,
    )
    with TestClient(crear_app(Settings(), puertos=puertos)) as cliente:
        yield cliente


def test_la_ruta_usa_el_deadline_recibido_y_responde_503(api_lenta: TestClient) -> None:
    inicio = time.perf_counter()
    respuesta = api_lenta.post(
        "/v1/cotizaciones", json=C01, headers={**ASESOR, **clave(), "X-Deadline-Ms": "90"}
    )
    duracion_ms = (time.perf_counter() - inicio) * 1000
    assert respuesta.status_code == 503, respuesta.text
    assert respuesta.json()["codigo"] == "DEPENDENCIA_NO_DISPONIBLE"
    assert respuesta.json()["detalles"] == {"dependencia": DEPENDENCIA_PRODUCTO}
    assert duracion_ms <= 90 + 2 * TOLERANCIA_MS, duracion_ms


@pytest.mark.parametrize("valor", ["0", "-1", "abc"])
def test_un_deadline_invalido_es_422(api_lenta: TestClient, valor: str) -> None:
    respuesta = api_lenta.post(
        "/v1/cotizaciones", json=C01, headers={**ASESOR, **clave(), "X-Deadline-Ms": valor}
    )
    assert respuesta.status_code == 422
