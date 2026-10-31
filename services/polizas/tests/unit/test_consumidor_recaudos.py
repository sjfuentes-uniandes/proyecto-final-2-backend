"""Consumidor de `polizas-recaudos` en polizas (HU-W16, 16.13): configuración y ciclo de vida.

La infraestructura entrega `SQS_POLIZAS_RECAUDOS_URL` a polizas (`consumes`, `infra/apps/ecs.tf:82`)
y `AWS_REGION` a todos. Sin la cola (local y pruebas) el consumidor no arranca. Con ella, corre como
tarea del ciclo de vida sobre el pool del servicio; si falta la base, o el consumidor falla, la
aplicación sigue sirviendo. Al cerrar se detiene antes que el relay y que el pool.
"""

import asyncio
import io
import json
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, ClassVar, Self

import psycopg_pool
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from polizas import main
from polizas.adapters.outbound import decisiones_pg
from polizas.adapters.outbound.polizas_pg import PolizasPg
from polizas.adapters.outbound.reloj import RelojSistema
from polizas.config import Settings
from solventa_common import inbox

COLA = "https://sqs.us-east-1.amazonaws.com/000000000000/solventa-int-polizas-recaudos"
TOPICO = "arn:aws:sns:us-east-1:000000000000:solventa-int-eventos-negocio"
_CON_ALIAS = ("SQS_POLIZAS_RECAUDOS_URL", "AWS_REGION", "AWS_ENDPOINT_URL", "OUTBOX_ENABLED")
_SIN_ALIAS = ("CONSUMIDOR_MAX_MENSAJES", "CONSUMIDOR_ESPERA_LARGA_S")


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in _CON_ALIAS:
        monkeypatch.delenv(nombre, raising=False)
    for nombre in _CON_ALIAS + _SIN_ALIAS:
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)


# --- Configuración ---------------------------------------------------------------------------------
def test_por_omision_sin_cola_lote_de_la_libreria_y_espera_de_10_s() -> None:
    settings = Settings()
    assert settings.sqs_polizas_recaudos_url is None
    assert settings.consumidor_max_mensajes == inbox.MAX_MENSAJES
    assert settings.consumidor_espera_larga_s == 10


def test_la_cola_como_la_pone_ecs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQS_POLIZAS_RECAUDOS_URL", COLA)
    assert Settings().sqs_polizas_recaudos_url == COLA


def test_variantes_solventa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLVENTA_SQS_POLIZAS_RECAUDOS_URL", COLA)
    monkeypatch.setenv("SOLVENTA_CONSUMIDOR_MAX_MENSAJES", "10")
    monkeypatch.setenv("SOLVENTA_CONSUMIDOR_ESPERA_LARGA_S", "1")
    settings = Settings()
    assert settings.sqs_polizas_recaudos_url == COLA
    assert (settings.consumidor_max_mensajes, settings.consumidor_espera_larga_s) == (10, 1)


@pytest.mark.parametrize(
    "campo",
    [
        {"consumidor_max_mensajes": 0},
        {"consumidor_max_mensajes": 11},
        {"consumidor_espera_larga_s": 0},
        {"consumidor_espera_larga_s": 11},
    ],
)
def test_lote_y_espera_acotados(campo: dict[str, int]) -> None:
    # Lote: el máximo de SQS. Espera: hasta 10 s, para que la parada quepa en el stopTimeout.
    with pytest.raises(ValueError):
        Settings(**campo)


def test_la_parada_del_consumidor_y_el_relay_cabe_en_el_stop_timeout_de_ecs() -> None:
    # infra/apps/ecs.tf:141: stopTimeout = 30 s. En serie: primero el consumidor y luego el relay;
    # 5 s de margen para uvicorn y los cierres.
    assert main.PARADA_CONSUMIDOR_S == 15
    assert main.PARADA_CONSUMIDOR_S > Settings().consumidor_espera_larga_s
    assert main.PARADA_CONSUMIDOR_S + main.PARADA_RELAY_S <= 25


# --- Ciclo de vida ---------------------------------------------------------------------------------
class PoolFalso:
    def __init__(self, url: str = "", **argumentos: Any) -> None:
        self.abierto = False

    async def open(self) -> None:
        self.abierto = True

    async def close(self) -> None:
        self.abierto = False

    def connection(self) -> Any:
        class Conexion:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def execute(self, sql: str) -> None:
                return None

        return Conexion()


class ConsumidorFalso:
    """Registra con qué se construyó; `comportamiento` decide qué hace `ejecutar`."""

    creados: ClassVar[list["ConsumidorFalso"]] = []
    comportamiento: ClassVar[str] = "espera"

    def __init__(self, receptor: Any, repositorio: Any, manejador: Any, **opciones: Any) -> None:
        self.receptor = receptor
        self.repositorio = repositorio
        self.manejador = manejador
        self.opciones = opciones
        self.parada_vista = False
        self.al_parar: dict[str, Any] = {}
        ConsumidorFalso.creados.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        if ConsumidorFalso.comportamiento == "falla":
            raise RuntimeError(f"fallo inesperado con {COLA}")
        if ConsumidorFalso.comportamiento == "ignora_parada":
            await asyncio.sleep(3600)
        await parada.wait()
        self.parada_vista = True
        relay = getattr(_app_actual, "relay_outbox", None)
        self.al_parar = {
            "pool_abierto": self.repositorio._pool.abierto,
            "relay_vivo": relay is not None and not relay.done(),
        }


_app_actual: Any = None


@pytest.fixture
def clientes_sqs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    llamadas: list[tuple[Any, ...]] = []

    def cliente(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        llamadas.append((region, endpoint_url, espera_larga_s))
        return SimpleNamespace(meta=SimpleNamespace(config=SimpleNamespace(read_timeout=60)))

    monkeypatch.setattr(main, "cliente_sqs", cliente)
    monkeypatch.setattr(main, "cliente_sns", lambda region, *, endpoint_url=None: object())
    return llamadas


@pytest.fixture
def consumidor_falso(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[ConsumidorFalso]]:
    ConsumidorFalso.creados = []
    ConsumidorFalso.comportamiento = "espera"
    monkeypatch.setattr(main, "ConsumidorInbox", ConsumidorFalso)
    yield ConsumidorFalso


@pytest.fixture
def crear(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., io.StringIO]
) -> Callable[..., tuple[FastAPI, io.StringIO]]:
    """La app con la base configurada (pool falso) y los logs capturados tras instrumentar."""
    monkeypatch.setattr(decisiones_pg, "migrar", lambda url: None)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)

    def _crear(con_base: bool = True, **cambios: Any) -> tuple[FastAPI, io.StringIO]:
        global _app_actual
        # HU-W17 (17.8): la tarea emisora se prueba en test_emisora.py.
        cambios = {"emision_habilitada": False} | cambios
        settings = Settings(DB_HOST="rds.interno" if con_base else None, **cambios)
        app = main.crear_app(settings)
        _app_actual = app.state
        return app, capturar_logs()

    return _crear


def _logs(salida: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in salida.getvalue().splitlines() if linea.strip()]


def _mensajes(salida: io.StringIO, nivel: str) -> list[dict[str, Any]]:
    return [log for log in _logs(salida) if log["level"] == nivel]


def test_sin_cola_no_crea_la_tarea(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    consumidor_falso: type[ConsumidorFalso],
    clientes_sqs: list[tuple[Any, ...]],
) -> None:
    app, salida = crear()
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "consumidor_recaudos", None) is None
    assert consumidor_falso.creados == []
    assert clientes_sqs == []
    assert [log["message"] for log in _mensajes(salida, "INFO")].count(
        main.MENSAJE_CONSUMIDOR_SIN_COLA
    ) == 1
    assert _mensajes(salida, "WARNING") == _mensajes(salida, "ERROR") == []


class RelayFalso:
    def __init__(self, *_: Any, **__: Any) -> None:
        return None

    async def ejecutar(self, parada: asyncio.Event) -> None:
        await parada.wait()


def test_con_cola_arranca_sobre_el_pool_y_se_detiene_antes_que_el_relay(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    consumidor_falso: type[ConsumidorFalso],
    clientes_sqs: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "RelayOutbox", RelayFalso)
    app, salida = crear(
        SQS_POLIZAS_RECAUDOS_URL=COLA,
        AWS_REGION="us-east-2",
        AWS_ENDPOINT_URL="http://localstack:4566",
        OUTBOX_ENABLED=True,
        EVENTS_TOPIC_ARN=TOPICO,
        consumidor_max_mensajes=3,
        consumidor_espera_larga_s=4,
    )
    with TestClient(app) as cliente:
        tarea = app.state.consumidor_recaudos
        assert isinstance(tarea, asyncio.Task) and not tarea.done()
        assert cliente.get("/health").json() == {"status": "ok"}
        pool = app.state.pool
        relay = app.state.relay_outbox
    [consumidor] = consumidor_falso.creados
    assert clientes_sqs == [("us-east-2", "http://localstack:4566", 4)]
    receptor = consumidor.receptor
    assert isinstance(receptor, inbox.ReceptorSqs)
    assert (receptor._cola_url, receptor.espera_larga_s, receptor.max_mensajes) == (COLA, 4, 3)
    assert isinstance(consumidor.repositorio, inbox.InboxPg)
    assert consumidor.repositorio._pool is pool
    assert callable(consumidor.manejador)
    assert consumidor.opciones["reloj"]().utcoffset() is not None
    # Se detuvo por la señal (no cancelado), con el relay aún vivo y el pool abierto.
    assert tarea.done() and not tarea.cancelled()
    assert consumidor.parada_vista is True
    assert consumidor.al_parar == {"pool_abierto": True, "relay_vivo": True}
    assert relay.done() and not relay.cancelled()
    assert _mensajes(salida, "ERROR") == []


def test_con_puertos_inyectados_usa_el_pool_y_el_reloj_dados(
    consumidor_falso: type[ConsumidorFalso],
    clientes_sqs: list[tuple[Any, ...]],
    dobles: Any,
) -> None:
    # El camino del arnés de módulo: puertos de prueba y el pool que abre la prueba.
    global _app_actual
    pool = PoolFalso()
    app = main.crear_app(
        Settings(SQS_POLIZAS_RECAUDOS_URL=COLA), puertos=dobles.puertos(), pool=pool
    )
    _app_actual = app.state
    with TestClient(app):
        pool.abierto = True
    [consumidor] = consumidor_falso.creados
    assert consumidor.repositorio._pool is pool
    assert consumidor.opciones["reloj"]() == dobles.reloj.ahora()
    assert consumidor.parada_vista is True


def test_con_cola_y_sin_base_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    consumidor_falso: type[ConsumidorFalso],
    clientes_sqs: list[tuple[Any, ...]],
) -> None:
    app, salida = crear(con_base=False, SQS_POLIZAS_RECAUDOS_URL=COLA)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert consumidor_falso.creados == []
    [error] = _mensajes(salida, "ERROR")
    assert (error["message"], error["missing"]) == (
        main.MENSAJE_CONSUMIDOR_SIN_CONFIGURACION,
        "base",
    )


def test_si_no_se_puede_crear_el_cliente_sqs_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    consumidor_falso: type[ConsumidorFalso],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cliente(region: str, *, endpoint_url: str | None = None, espera_larga_s: int) -> Any:
        raise ValueError(f"Invalid endpoint: {endpoint_url}")

    monkeypatch.setattr(main, "cliente_sqs", cliente)
    app, salida = crear(SQS_POLIZAS_RECAUDOS_URL=COLA, AWS_ENDPOINT_URL="no-url")
    with TestClient(app) as cliente_http:
        assert cliente_http.get("/health").json() == {"status": "ok"}
    assert consumidor_falso.creados == []
    [error] = _mensajes(salida, "ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_CONSUMIDOR_NO_ARRANCO,
        "ValueError",
    )
    assert "no-url" not in salida.getvalue()


def test_un_consumidor_que_falla_no_tumba_la_aplicacion(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    consumidor_falso: type[ConsumidorFalso],
    clientes_sqs: list[tuple[Any, ...]],
) -> None:
    consumidor_falso.comportamiento = "falla"
    app, salida = crear(SQS_POLIZAS_RECAUDOS_URL=COLA)
    with TestClient(app) as cliente:
        tarea = app.state.consumidor_recaudos
        _esperar(lambda: tarea.done())
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = _mensajes(salida, "ERROR")
    assert (error["message"], error["error_type"]) == (
        main.MENSAJE_CONSUMIDOR_DETENIDO,
        "RuntimeError",
    )
    assert COLA not in salida.getvalue()


def test_la_parada_espera_con_limite_y_luego_cancela(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    consumidor_falso: type[ConsumidorFalso],
    clientes_sqs: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumidor_falso.comportamiento = "ignora_parada"
    monkeypatch.setattr(main, "PARADA_CONSUMIDOR_S", 0.05)
    app, salida = crear(SQS_POLIZAS_RECAUDOS_URL=COLA)
    inicio = time.monotonic()
    with TestClient(app):
        tarea = app.state.consumidor_recaudos
    assert time.monotonic() - inicio < 5
    assert tarea.cancelled()
    [aviso] = _mensajes(salida, "WARNING")
    assert (aviso["message"], aviso["limit_s"]) == (main.MENSAJE_CONSUMIDOR_SIN_PARAR, 0.05)


def test_un_consumidor_real_procesa_la_cola_con_el_manejador_de_recaudos(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    clientes_sqs: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Consumidor real con un receptor vacío: los ciclos corren (sin mensajes) y el cierre lo
    # detiene por la señal; el manejador es el de recaudos.
    from polizas.adapters.inbound.sqs import recaudos

    construidos: list[Any] = []
    original = main.manejador_recaudos

    def manejador(caso: Any) -> Any:
        construidos.append(caso)
        return original(caso)

    class ReceptorVacio:
        def __init__(self, *_: Any, **__: Any) -> None:
            self.recepciones = 0

        async def recibir(self) -> list[Any]:
            self.recepciones += 1
            await asyncio.sleep(0.01)
            return []

        async def borrar(self, mensaje: Any) -> None:
            return None

    monkeypatch.setattr(main, "manejador_recaudos", manejador)
    monkeypatch.setattr(main, "ReceptorSqs", ReceptorVacio)
    app, salida = crear(SQS_POLIZAS_RECAUDOS_URL=COLA)
    with TestClient(app):
        tarea = app.state.consumidor_recaudos
        _esperar(lambda: not tarea.done() and len(construidos) == 1)
        time.sleep(0.05)
    assert tarea.done() and not tarea.cancelled()
    [caso] = construidos
    assert caso._proyeccion._pool is app.state.pool
    # HU-W17 (17.6): la póliza se crea en la misma transacción, con el reloj del sistema.
    assert isinstance(caso._polizas, PolizasPg)
    assert isinstance(caso._reloj, RelojSistema)
    assert recaudos.manejador_recaudos is original
    assert _mensajes(salida, "ERROR") == []


def _esperar(condicion: Callable[[], bool], limite_s: float = 5) -> None:
    fin = time.monotonic() + limite_s
    while not condicion():
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        time.sleep(0.01)
