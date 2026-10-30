"""Relay del outbox en polizas (HU-W16, 16.6; P16-1): configuración y ciclo de vida.

La infraestructura pone `OUTBOX_ENABLED = "true"` y `EVENTS_TOPIC_ARN` a todo servicio que publica
(`infra/apps/ecs.tf:81`) y `AWS_REGION` a todos (`ecs.tf:60`). Sin la variable (local y pruebas)
el relay no arranca. Habilitado, corre como tarea del ciclo de vida sobre el pool del servicio; si
falta el tópico o la base, o el relay falla, la aplicación sigue sirviendo.
"""

import asyncio
import io
import json
import time
from collections.abc import Callable, Iterator
from typing import Any, ClassVar, Self

import psycopg_pool
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from polizas import main
from polizas.adapters.outbound import decisiones_pg
from polizas.config import Settings
from solventa_common import outbox

TOPICO = "arn:aws:sns:us-east-1:000000000000:solventa-int-eventos-negocio"
_CON_ALIAS = ("OUTBOX_ENABLED", "EVENTS_TOPIC_ARN", "AWS_REGION", "AWS_ENDPOINT_URL")
_SIN_ALIAS = ("OUTBOX_LOTE", "OUTBOX_INTERVALO_S", "OUTBOX_MAX_INTENTOS")


@pytest.fixture(autouse=True)
def sin_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in _CON_ALIAS:
        monkeypatch.delenv(nombre, raising=False)
    for nombre in _CON_ALIAS + _SIN_ALIAS:
        monkeypatch.delenv(f"SOLVENTA_{nombre}", raising=False)


# --- Configuración ---------------------------------------------------------------------------------
def test_por_omision_apagado_con_los_valores_de_la_libreria() -> None:
    settings = Settings()
    assert settings.outbox_enabled is False
    assert settings.events_topic_arn is None
    assert (settings.outbox_lote, settings.outbox_intervalo_s, settings.outbox_max_intentos) == (
        outbox.LOTE,
        outbox.INTERVALO_S,
        outbox.MAX_INTENTOS,
    )
    assert (settings.aws_region, settings.aws_endpoint_url) == ("us-east-1", None)


@pytest.mark.parametrize(("valor", "esperado"), [("true", True), ("false", False)])
def test_las_variables_como_las_pone_ecs(
    monkeypatch: pytest.MonkeyPatch, valor: str, esperado: bool
) -> None:
    monkeypatch.setenv("OUTBOX_ENABLED", valor)
    monkeypatch.setenv("EVENTS_TOPIC_ARN", TOPICO)
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    settings = Settings()
    assert settings.outbox_enabled is esperado
    assert (settings.events_topic_arn, settings.aws_region) == (TOPICO, "us-east-2")
    assert settings.aws_endpoint_url is None


def test_variantes_solventa_como_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    # compose.yaml inyecta SOLVENTA_AWS_ENDPOINT_URL (LocalStack); los tiempos no vienen de infra.
    monkeypatch.setenv("SOLVENTA_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("SOLVENTA_EVENTS_TOPIC_ARN", TOPICO)
    monkeypatch.setenv("SOLVENTA_AWS_ENDPOINT_URL", "http://localstack:4566")
    monkeypatch.setenv("SOLVENTA_OUTBOX_LOTE", "10")
    monkeypatch.setenv("SOLVENTA_OUTBOX_INTERVALO_S", "0.5")
    monkeypatch.setenv("SOLVENTA_OUTBOX_MAX_INTENTOS", "3")
    settings = Settings()
    assert (settings.outbox_enabled, settings.events_topic_arn) == (True, TOPICO)
    assert settings.aws_endpoint_url == "http://localstack:4566"
    assert (settings.outbox_lote, settings.outbox_intervalo_s, settings.outbox_max_intentos) == (
        10,
        0.5,
        3,
    )


def test_sin_topico_la_configuracion_no_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTBOX_ENABLED", "true")
    settings = Settings()
    assert (settings.outbox_enabled, settings.events_topic_arn) == (True, None)


@pytest.mark.parametrize(
    "campo", [{"outbox_lote": 0}, {"outbox_max_intentos": 0}, {"outbox_intervalo_s": 0}]
)
def test_lote_intentos_e_intervalo_positivos(campo: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        Settings(**campo)


# --- Ciclo de vida ---------------------------------------------------------------------------------
class PoolFalso:
    """Atiende /health; no tiene transacciones, así que un relay real falla cada ciclo."""

    def __init__(self, url: str, **argumentos: Any) -> None:
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


class RelayFalso:
    """Registra con qué se construyó; `comportamiento` decide qué hace `ejecutar`."""

    creados: ClassVar[list["RelayFalso"]] = []
    comportamiento: ClassVar[str] = "espera"

    def __init__(self, repositorio: Any, publicador: Any, **opciones: Any) -> None:
        self.repositorio = repositorio
        self.publicador = publicador
        self.opciones = opciones
        self.parada_vista = False
        self.pool_abierto_al_parar: bool | None = None
        RelayFalso.creados.append(self)

    async def ejecutar(self, parada: asyncio.Event) -> None:
        if RelayFalso.comportamiento == "falla":
            raise RuntimeError("fallo inesperado del relay")
        if RelayFalso.comportamiento == "ignora_parada":
            await asyncio.sleep(3600)
        await parada.wait()
        self.parada_vista = True
        self.pool_abierto_al_parar = self.repositorio._pool.abierto


@pytest.fixture
def clientes_sns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    llamadas: list[tuple[Any, ...]] = []

    def cliente(region: str, *, endpoint_url: str | None = None) -> object:
        llamadas.append((region, endpoint_url))
        return object()

    monkeypatch.setattr(main, "cliente_sns", cliente)
    return llamadas


@pytest.fixture
def relay_falso(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[RelayFalso]]:
    RelayFalso.creados = []
    RelayFalso.comportamiento = "espera"
    monkeypatch.setattr(main, "RelayOutbox", RelayFalso)
    yield RelayFalso


@pytest.fixture
def crear(
    monkeypatch: pytest.MonkeyPatch, capturar_logs: Callable[..., io.StringIO]
) -> Callable[..., tuple[FastAPI, io.StringIO]]:
    """La app con la base configurada (pool falso) y los logs capturados tras instrumentar."""
    monkeypatch.setattr(decisiones_pg, "migrar", lambda url: None)
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", PoolFalso)

    def _crear(con_base: bool = True, **cambios: Any) -> tuple[FastAPI, io.StringIO]:
        # HU-W17 (17.8): la tarea emisora se prueba en test_emisora.py.
        cambios = {"emision_habilitada": False} | cambios
        settings = Settings(DB_HOST="rds.interno" if con_base else None, **cambios)
        app = main.crear_app(settings)
        return app, capturar_logs()

    return _crear


def _logs(salida: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in salida.getvalue().splitlines() if linea.strip()]


def _mensajes(salida: io.StringIO, nivel: str) -> list[dict[str, Any]]:
    return [log for log in _logs(salida) if log["level"] == nivel]


def test_deshabilitado_no_crea_la_tarea(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    relay_falso: type[RelayFalso],
    clientes_sns: list[tuple[Any, ...]],
) -> None:
    app, salida = crear(EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "relay_outbox", None) is None
    assert relay_falso.creados == []
    assert clientes_sns == []
    assert _mensajes(salida, "ERROR") == []


def test_habilitado_arranca_sobre_el_pool_y_se_detiene_al_cerrar(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    relay_falso: type[RelayFalso],
    clientes_sns: list[tuple[Any, ...]],
) -> None:
    app, salida = crear(
        OUTBOX_ENABLED=True,
        EVENTS_TOPIC_ARN=TOPICO,
        AWS_REGION="us-east-2",
        AWS_ENDPOINT_URL="http://localstack:4566",
        outbox_lote=7,
        outbox_intervalo_s=0.5,
        outbox_max_intentos=3,
    )
    with TestClient(app) as cliente:
        tarea = app.state.relay_outbox
        assert isinstance(tarea, asyncio.Task) and not tarea.done()
        assert cliente.get("/health").json() == {"status": "ok"}
        pool = app.state.pool
    [relay] = relay_falso.creados
    assert isinstance(relay.repositorio, outbox.OutboxPg)
    assert relay.repositorio._pool is pool
    assert isinstance(relay.publicador, outbox.PublicadorSns)
    assert relay.publicador._topic_arn == TOPICO
    assert clientes_sns == [("us-east-2", "http://localstack:4566")]
    assert {k: relay.opciones[k] for k in ("lote", "intervalo_s", "max_intentos")} == {
        "lote": 7,
        "intervalo_s": 0.5,
        "max_intentos": 3,
    }
    assert relay.opciones["reloj"]().utcoffset() is not None
    # Se detuvo por la señal (no cancelado) y antes de cerrar el pool.
    assert tarea.done() and not tarea.cancelled()
    assert (relay.parada_vista, relay.pool_abierto_al_parar) == (True, True)
    assert _mensajes(salida, "ERROR") == []


def test_con_puertos_inyectados_usa_el_pool_y_el_reloj_dados(
    relay_falso: type[RelayFalso],
    clientes_sns: list[tuple[Any, ...]],
    dobles: Any,
) -> None:
    # El camino del arnés de módulo: puertos de prueba y el pool que abre la prueba.
    pool = PoolFalso("postgresql://prueba")
    app = main.crear_app(
        Settings(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO), puertos=dobles.puertos(), pool=pool
    )
    with TestClient(app) as cliente:
        assert app.state.pool is pool
        assert cliente.get("/health").json() == {"status": "ok"}
    [relay] = relay_falso.creados
    assert relay.repositorio._pool is pool
    assert relay.opciones["reloj"]() == dobles.reloj.ahora()
    assert relay.parada_vista is True


@pytest.mark.parametrize("topico", [None, ""])
def test_habilitado_sin_topico_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    relay_falso: type[RelayFalso],
    clientes_sns: list[tuple[Any, ...]],
    topico: str | None,
) -> None:
    app, salida = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=topico)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
        assert getattr(app.state, "relay_outbox", None) is None
    assert relay_falso.creados == []
    [error] = _mensajes(salida, "ERROR")
    assert error["message"] == main.MENSAJE_RELAY_SIN_CONFIGURACION
    assert error["missing"] == "EVENTS_TOPIC_ARN"


def test_habilitado_sin_base_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    relay_falso: type[RelayFalso],
    clientes_sns: list[tuple[Any, ...]],
) -> None:
    app, salida = crear(con_base=False, OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app) as cliente:
        assert cliente.get("/health").json() == {"status": "ok"}
    assert relay_falso.creados == []
    [error] = _mensajes(salida, "ERROR")
    assert (error["message"], error["missing"]) == (main.MENSAJE_RELAY_SIN_CONFIGURACION, "base")


def test_si_no_se_puede_crear_el_cliente_sns_arranca_y_lo_registra(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    relay_falso: type[RelayFalso],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cliente(region: str, *, endpoint_url: str | None = None) -> object:
        raise ValueError(f"Invalid endpoint: {endpoint_url}")

    monkeypatch.setattr(main, "cliente_sns", cliente)
    app, salida = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO, AWS_ENDPOINT_URL="no-url")
    with TestClient(app) as cliente_http:
        assert cliente_http.get("/health").json() == {"status": "ok"}
    assert relay_falso.creados == []
    [error] = _mensajes(salida, "ERROR")
    assert (error["message"], error["error_type"]) == (main.MENSAJE_RELAY_NO_ARRANCO, "ValueError")
    assert "no-url" not in salida.getvalue()


def test_un_relay_que_falla_no_tumba_la_aplicacion(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    relay_falso: type[RelayFalso],
    clientes_sns: list[tuple[Any, ...]],
) -> None:
    relay_falso.comportamiento = "falla"
    app, salida = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    with TestClient(app) as cliente:
        tarea = app.state.relay_outbox
        _esperar(lambda: tarea.done())
        assert cliente.get("/health").json() == {"status": "ok"}
    [error] = _mensajes(salida, "ERROR")
    assert (error["message"], error["error_type"]) == (main.MENSAJE_RELAY_DETENIDO, "RuntimeError")
    assert "fallo inesperado" not in salida.getvalue()


def test_un_relay_real_con_ciclos_fallidos_sigue_y_la_app_sirve(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    clientes_sns: list[tuple[Any, ...]],
) -> None:
    # Relay real sobre el pool falso: cada ciclo falla (sin transacciones), lo registra la
    # librería y la espera crece; la aplicación responde y el cierre lo detiene por la señal.
    app, salida = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO, outbox_intervalo_s=0.01)
    with TestClient(app) as cliente:
        tarea = app.state.relay_outbox
        _esperar(
            lambda: any(log["message"] == outbox.MENSAJE_CICLO_FALLIDO for log in _logs(salida))
        )
        assert cliente.get("/health").json() == {"status": "ok"}
        assert not tarea.done()
    assert tarea.done() and not tarea.cancelled()
    assert {log["message"] for log in _mensajes(salida, "ERROR")} == {outbox.MENSAJE_CICLO_FALLIDO}


def test_la_parada_espera_con_limite_y_luego_cancela(
    crear: Callable[..., tuple[FastAPI, io.StringIO]],
    relay_falso: type[RelayFalso],
    clientes_sns: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay_falso.comportamiento = "ignora_parada"
    monkeypatch.setattr(main, "PARADA_RELAY_S", 0.05)
    app, salida = crear(OUTBOX_ENABLED=True, EVENTS_TOPIC_ARN=TOPICO)
    inicio = time.monotonic()
    with TestClient(app):
        tarea = app.state.relay_outbox
    assert time.monotonic() - inicio < 5
    assert tarea.cancelled()
    [aviso] = _mensajes(salida, "WARNING")
    assert (aviso["message"], aviso["limit_s"]) == (main.MENSAJE_RELAY_SIN_PARAR, 0.05)


def test_el_limite_de_parada_cabe_en_el_stop_timeout_de_ecs() -> None:
    # infra/apps/ecs.tf:141: stopTimeout = 30 s entre SIGTERM y SIGKILL.
    assert main.PARADA_RELAY_S == 10
    assert main.PARADA_RELAY_S < 30


def _esperar(condicion: Callable[[], bool], limite_s: float = 5) -> None:
    fin = time.monotonic() + limite_s
    while not condicion():
        assert time.monotonic() < fin, "la condición no se cumplió a tiempo"
        time.sleep(0.01)
